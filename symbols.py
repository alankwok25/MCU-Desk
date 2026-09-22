"""ELF/DWARF variable descriptions. Addresses are rebuilt for every firmware."""
from dataclasses import dataclass, field, replace
from io import BytesIO
from pathlib import Path
import re
import struct


@dataclass
class Type:
    name: str = '未知类型'
    kind: str = 'unknown'
    size: int = 0
    fmt: object = None
    members: list = field(default_factory=list)
    element: object = None
    count: int = 0
    enums: dict = field(default_factory=dict)


@dataclass
class Variable:
    path: str
    address: int
    type: Type

    @property
    def selectable(self):
        return self.type.fmt is not None and self.type.size > 0

    def children(self, limit=256):
        if self.type.kind in ('struct', 'union'):
            return [Variable(self.path + '.' + name, self.address + offset, typ)
                    for name, offset, typ in self.type.members[:limit]]
        if self.type.kind == 'array':
            return [Variable('%s[%d]' % (self.path, i), self.address + i * self.type.element.size,
                             self.type.element) for i in range(min(limit, self.type.count))]
        return []


def decode_value(data, fmt, byte_order):
    if isinstance(fmt, str):
        return struct.unpack(byte_order + fmt, data)[0]
    value = int.from_bytes(data, 'little' if byte_order == '<' else 'big')
    value = (value >> fmt['shift']) & ((1 << fmt['bits']) - 1)
    if fmt.get('signed') and value & (1 << (fmt['bits'] - 1)):
        value -= 1 << fmt['bits']
    return value


class SymbolIndex:
    def __init__(self, path):
        from elftools.elf.elffile import ELFFile
        from elftools.dwarf.dwarf_expr import DWARFExprParser
        self.path = str(Path(path).resolve())
        self.stream = BytesIO(Path(path).read_bytes())
        self.elf = ELFFile(self.stream)
        self.byte_order = '<' if self.elf.little_endian else '>'
        self.roots = {}
        self.rtt_address = None
        self.types = {}
        self.warnings = []
        symbols = self.elf.get_section_by_name('.symtab')
        entries = [] if symbols is None else [s for s in symbols.iter_symbols()
                    if s['st_info']['type'] == 'STT_OBJECT' and s['st_shndx'] != 'SHN_UNDEF']
        by_name = {}
        for symbol in entries:
            by_name.setdefault(symbol.name, []).append(symbol)
            if symbol.name == '_SEGGER_RTT':
                self.rtt_address = symbol['st_value']
        if self.elf.has_dwarf_info(strict=True):
            self.dwarf = self.elf.get_dwarf_info()
            seen = set()
            for cu in self.dwarf.iter_CUs():
                source = self.attr(cu.get_top_DIE(), 'DW_AT_name')
                source = self.text(source).replace('\\', '/').rsplit('/', 1)[-1]
                for die in cu.iter_DIEs():
                    # A definition may refer to an extern declaration via DW_AT_specification.
                    # Its declaration flag belongs to that declaration, not to the definition.
                    declaration = die.attributes.get('DW_AT_declaration')
                    if die.tag != 'DW_TAG_variable' or (declaration is not None and declaration.value):
                        continue
                    name = self.text(self.attr(die, 'DW_AT_name'))
                    if not name:
                        continue
                    loc = self.attr(die, 'DW_AT_location')
                    address = None
                    if isinstance(loc, (list, bytes)):
                        try:
                            ops = DWARFExprParser(cu.structs).parse_expr(loc)
                            if len(ops) == 1 and ops[0].op_name == 'DW_OP_addr':
                                address = ops[0].args[0]
                        except Exception:
                            pass
                    # Never use a symbol to guess the location of an optimized stack variable.
                    if address is None and loc is None and self.attr(die, 'DW_AT_external'):
                        candidates = by_name.get(name, [])
                        if len(candidates) == 1:
                            address = candidates[0]['st_value']
                    if address is None or (name, address) in seen:
                        continue
                    typ = self.type_of(self.reference(die, 'DW_AT_type'))
                    seen.add((name, address))
                    key = name
                    if key in self.roots:
                        key = '%s::%s' % (source, name)
                    if key in self.roots:
                        key += '@%X' % address
                    self.roots[key] = Variable(key, address, typ)
        else:
            self.warnings.append('固件没有 DWARF 调试信息，无法自动解析成员类型；请启用编译器调试信息。')
        known = {(v.path.rsplit('::', 1)[-1], v.address) for v in self.roots.values()}
        for symbol in entries:
            if (symbol.name, symbol['st_value']) not in known and symbol.name not in self.roots:
                self.roots[symbol.name] = Variable(symbol.name, symbol['st_value'],
                                                   Type('缺少类型信息', size=symbol['st_size']))
        self.roots = dict(sorted(self.roots.items(), key=lambda item: item[0].lower()))

    @staticmethod
    def text(value):
        return value.decode('utf-8', 'replace') if isinstance(value, bytes) else str(value or '')

    def owner(self, die, key, seen=None):
        if die is None:
            return None
        seen = set() if seen is None else seen
        if die.offset in seen:
            return None
        seen.add(die.offset)
        if key in die.attributes:
            return die
        for ref in ('DW_AT_specification', 'DW_AT_abstract_origin'):
            if ref in die.attributes:
                result = self.owner(die.get_DIE_from_attribute(ref), key, seen)
                if result is not None:
                    return result
        return None

    def attr(self, die, key, default=None):
        owner = self.owner(die, key)
        return owner.attributes[key].value if owner is not None else default

    def reference(self, die, key):
        owner = self.owner(die, key)
        return owner.get_DIE_from_attribute(key) if owner is not None else None

    def type_of(self, die):
        from elftools.dwarf.dwarf_expr import DWARFExprParser
        if die is None:
            return Type()
        if die.offset in self.types:
            return self.types[die.offset]
        typ = Type(self.text(self.attr(die, 'DW_AT_name')) or '匿名类型',
                   size=self.attr(die, 'DW_AT_byte_size', 0))
        self.types[die.offset] = typ
        tag = die.tag
        if tag in ('DW_TAG_typedef', 'DW_TAG_const_type', 'DW_TAG_volatile_type', 'DW_TAG_restrict_type', 'DW_TAG_atomic_type'):
            target = self.type_of(self.reference(die, 'DW_AT_type'))
            typ = replace(target, name=typ.name if tag == 'DW_TAG_typedef' else target.name)
            self.types[die.offset] = typ
        elif tag in ('DW_TAG_base_type', 'DW_TAG_enumeration_type', 'DW_TAG_pointer_type'):
            typ.kind = 'enum' if tag == 'DW_TAG_enumeration_type' else 'pointer' if tag == 'DW_TAG_pointer_type' else 'scalar'
            encoding = self.attr(die, 'DW_AT_encoding')
            if typ.kind == 'pointer':
                typ.size = typ.size or die.cu['address_size']
                typ.name = '指针（仅地址）'
                encoding = 7
            if typ.kind == 'enum':
                base = self.type_of(self.reference(die, 'DW_AT_type'))
                typ.size = typ.size or base.size
                typ.fmt = base.fmt
                typ.enums = {self.attr(c, 'DW_AT_const_value'): self.text(self.attr(c, 'DW_AT_name'))
                             for c in die.iter_children() if c.tag == 'DW_TAG_enumerator'}
                if encoding is None and typ.fmt is None:
                    encoding = 5 if any(n < 0 for n in typ.enums) else 7
            if encoding == 4:
                typ.fmt = {4: 'f', 8: 'd'}.get(typ.size)
            elif encoding in (2, 5, 6, 7, 8):
                unsigned = encoding in (2, 7, 8)
                typ.fmt = ({1: 'B', 2: 'H', 4: 'I', 8: 'Q'} if unsigned else
                           {1: 'b', 2: 'h', 4: 'i', 8: 'q'}).get(typ.size)
        elif tag in ('DW_TAG_structure_type', 'DW_TAG_union_type'):
            typ.kind = 'struct' if tag == 'DW_TAG_structure_type' else 'union'
            for i, child in enumerate(die.iter_children()):
                if child.tag != 'DW_TAG_member':
                    continue
                member = self.type_of(self.reference(child, 'DW_AT_type'))
                name = self.text(self.attr(child, 'DW_AT_name')) or 'anonymous_%d' % i
                offset = self.attr(child, 'DW_AT_data_member_location', 0 if typ.kind == 'union' else None)
                if isinstance(offset, (bytes, list)):
                    ops = DWARFExprParser(child.cu.structs).parse_expr(offset)
                    offset = ops[0].args[0] if len(ops) == 1 and ops[0].op_name in ('DW_OP_plus_uconst', 'DW_OP_constu') else None
                bits = self.attr(child, 'DW_AT_bit_size')
                bit_start = self.attr(child, 'DW_AT_data_bit_offset')
                if bits is not None and bit_start is not None:
                    offset = bit_start // 8
                    size = ((bit_start % 8) + bits + 7) // 8
                    shift = bit_start % 8 if self.byte_order == '<' else size * 8 - bit_start % 8 - bits
                    member = replace(member, size=size, fmt={'bits': bits, 'shift': shift,
                                     'signed': member.fmt in ('b', 'h', 'i', 'q')})
                elif bits is not None and offset is not None:
                    bit_offset = self.attr(child, 'DW_AT_bit_offset')
                    size = self.attr(child, 'DW_AT_byte_size', member.size)
                    if bit_offset is None or not size:
                        member = replace(member, fmt=None)
                    else:
                        shift = size * 8 - bit_offset - bits
                        member = replace(member, size=size, fmt={'bits': bits, 'shift': shift,
                                         'signed': member.fmt in ('b', 'h', 'i', 'q')})
                if isinstance(offset, int) and offset >= 0:
                    if isinstance(member.fmt, dict) and (member.fmt['shift'] < 0 or member.fmt['bits'] <= 0):
                        member = replace(member, fmt=None)
                    typ.members.append((name, offset, member))
        elif tag == 'DW_TAG_array_type':
            element = self.type_of(self.reference(die, 'DW_AT_type'))
            counts = []
            for child in die.iter_children():
                if child.tag == 'DW_TAG_subrange_type':
                    count = self.attr(child, 'DW_AT_count')
                    upper = self.attr(child, 'DW_AT_upper_bound')
                    lower = self.attr(child, 'DW_AT_lower_bound', 0)
                    counts.append(count if isinstance(count, int) else upper - lower + 1 if isinstance(upper, int) else 0)
            for count in reversed(counts or [0]):
                element = Type('%s[%d]' % (element.name, count), 'array', element.size * count,
                               element=element, count=count)
            self.types[die.offset] = typ = element
        return typ

    def resolve(self, path):
        root = next((name for name in sorted(self.roots, key=len, reverse=True)
                     if path == name or path.startswith(name + '.') or path.startswith(name + '[')), None)
        if root is None:
            raise KeyError(path)
        node = self.roots[root]
        rest = path[len(root):]
        while rest:
            if rest.startswith('.'):
                match = re.match(r'\.([^\.\[]+)', rest)
                if not match:
                    raise KeyError(path)
                name = match.group(1)
                member = next((m for m in node.type.members if m[0] == name), None)
                if member is None:
                    raise KeyError(path)
                node = Variable(node.path + '.' + name, node.address + member[1], member[2])
            else:
                match = re.match(r'\[(\d+)\]', rest)
                if not match or node.type.kind != 'array':
                    raise KeyError(path)
                index = int(match.group(1))
                if index >= node.type.count:
                    raise KeyError(path)
                node = Variable(node.path + match.group(0), node.address + index * node.type.element.size, node.type.element)
            rest = rest[len(match.group(0)):]
        return node
