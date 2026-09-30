"""OpenOCD flash programming with sector preservation and verified writes."""
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import hashlib
import re
import socket
import subprocess
import tempfile
import threading
import time

from projects import validate_ranges


MAX_IMAGE = 32 * 1024 * 1024


@dataclass
class Image:
    path: str
    segments: list
    sha256: str

    @property
    def size(self):
        return sum(len(data) for _, data in self.segments)


def read_image(path, bin_address=''):
    path = Path(path).resolve()
    raw = path.read_bytes()
    if len(raw) > MAX_IMAGE * 4:
        raise ValueError('固件文件过大')
    ext = path.suffix.lower()
    segments = []
    if ext in ('.elf', '.axf', '.out'):
        from elftools.elf.elffile import ELFFile
        with BytesIO(raw) as stream:
            elf = ELFFile(stream)
            segments = [(s['p_paddr'], s.data()) for s in elf.iter_segments()
                        if s['p_type'] == 'PT_LOAD' and s['p_filesz']]
    elif ext == '.bin':
        if not bin_address.strip():
            raise ValueError('BIN 文件必须填写烧录起始地址')
        segments = [(int(bin_address, 0), raw)]
    elif ext == '.hex':
        base, eof = 0, False
        for line in raw.decode('ascii').splitlines():
            line = line.strip()
            if not line:
                continue
            if eof or not line.startswith(':'):
                raise ValueError('HEX 文件格式错误')
            record = bytes.fromhex(line[1:])
            if len(record) < 5 or len(record) != record[0] + 5 or sum(record) & 255:
                raise ValueError('HEX 记录长度或校验和错误')
            count, address, kind = record[0], int.from_bytes(record[1:3], 'big'), record[3]
            data = record[4:-1]
            if kind == 0:
                if count:
                    segments.append((base + address, data))
            elif kind == 1 and count == 0:
                eof = True
            elif kind in (2, 4) and count == 2:
                base = int.from_bytes(data, 'big') << (4 if kind == 2 else 16)
            elif kind not in (3, 5) or count != 4:
                raise ValueError('不支持的 HEX 记录')
        if not eof:
            raise ValueError('HEX 文件缺少结束记录')
    else:
        raise ValueError('请选择 ELF / AXF / OUT / HEX / BIN 固件')
    merged = []
    for address, data in sorted(segments):
        if not 0 <= address < address + len(data) <= 0x100000000:
            raise ValueError('固件地址超出 32 位范围')
        if merged and address < merged[-1][0] + len(merged[-1][1]):
            raise ValueError('固件包含重叠地址段')
        if merged and address == merged[-1][0] + len(merged[-1][1]):
            merged[-1][1].extend(data)
        else:
            merged.append([address, bytearray(data)])
    if not merged or sum(len(d) for _, d in merged) > MAX_IMAGE:
        raise ValueError('固件没有可写入的数据或数据过大')
    return Image(str(path), [(a, bytes(d)) for a, d in merged], hashlib.sha256(raw).hexdigest())


def intersects(start, end, regions):
    return any(start < hi and end > lo for lo, hi in regions)


def validate_image(image, allowed, protected):
    validate_ranges(allowed)
    validate_ranges(protected)
    for address, data in image.segments:
        end = address + len(data)
        if allowed and not any(lo <= address and end <= hi for lo, hi in allowed):
            raise ValueError('固件段 0x%08X–0x%08X 超出允许写入范围' % (address, end - 1))
        if intersects(address, end, protected):
            raise ValueError('固件覆盖了受保护区域')


def parse_sectors(text, bank, base, bank_size):
    sectors = []
    for match in re.finditer(r'#\s*(\d+)\s*:\s*(0x[\da-fA-F]+)\s*\(\s*(0x[\da-fA-F]+)', text):
        number, offset, size = int(match[1]), int(match[2], 16), int(match[3], 16)
        address = base + offset
        if not size or not base <= address < address + size <= base + bank_size:
            raise ValueError('OpenOCD 返回的扇区范围无效')
        sectors.append((bank, number, address, size))
    if not sectors:
        raise ValueError('无法读取该芯片的扇区布局，未执行擦写')
    ordered = sorted(sectors, key=lambda s: s[2])
    if any(a[2] + a[3] > b[2] for a, b in zip(ordered, ordered[1:])):
        raise ValueError('OpenOCD 返回了重叠扇区')
    return ordered


def plan_sectors(image, sectors, protected):
    selected = []
    for bank, number, start, size in sectors:
        if any(start < a + len(d) and start + size > a for a, d in image.segments):
            if intersects(start, start + size, protected):
                raise ValueError('所需擦除扇区与保护区共享：0x%08X–0x%08X，未执行擦写' % (start, start + size - 1))
            selected.append((bank, number, start, size))
    for address, data in image.segments:
        covered = sum(max(0, min(address + len(data), s + n) - max(address, s)) for _, _, s, n in selected)
        if covered != len(data):
            raise ValueError('固件包含该芯片 Flash 范围以外的地址，未执行擦写')
    if sum(s[3] for s in selected) > MAX_IMAGE:
        raise ValueError('涉及的扇区过大')
    return selected


def merge_sector(original, start, segments):
    data = bytearray(original)
    for address, fragment in segments:
        lo, hi = max(start, address), min(start + len(data), address + len(fragment))
        if lo < hi:
            data[lo - start:hi - start] = fragment[lo - address:hi - address]
    return bytes(data)


def tcl_word(value):
    # Double-quoted Tcl word with every substitution character escaped.
    value = str(value).replace('\\', '/')
    if any(c in value for c in ('\n', '\r', '\x00')):
        raise ValueError('路径或参数包含无效字符')
    for char in ('$', '[', ']', '"', '{', '}'):
        value = value.replace(char, '\\' + char)
    return '"' + value + '"'


class OpenOCD:
    def __init__(self, config, directory, cancel):
        self.cancel = cancel
        self.process = None
        self.sock = None
        self.log = None
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        executable = config['openocd']
        if not executable or not Path(executable).is_file():
            raise ValueError('请在工程设置中选择 OpenOCD 可执行文件')
        arguments = [executable, '-c', 'bindto 127.0.0.1', '-c', 'gdb_port disabled',
                     '-c', 'telnet_port disabled', '-c', 'tcl_port %d' % port]
        if config.get('scripts_dir'):
            arguments += ['-s', config['scripts_dir']]
        interface = config.get('interface_config') or ('interface/jlink.cfg' if config['backend'] == 'jlink' else 'interface/cmsis-dap.cfg')
        arguments += ['-f', interface]
        if config.get('probe_uid'):
            arguments += ['-c', 'adapter serial ' + tcl_word(config['probe_uid'])]
        transport = 'swd' if config.get('mode', 'ARM SWD') == 'ARM SWD' else 'jtag'
        arguments += ['-c', 'transport select ' + transport, '-f', config['target_config'],
                      '-c', 'adapter speed %d' % config.get('speed_khz', 1000)]
        reset = config.get('reset_config', 'none')
        if reset not in ('none', 'srst_only', 'srst_only srst_nogate connect_assert_srst'):
            raise ValueError('复位配置无效')
        arguments += ['-c', 'reset_config ' + reset, '-c', 'init']
        try:
            self.log = open(Path(directory) / 'openocd.log', 'w+b')
            self.process = subprocess.Popen(arguments, cwd=directory, stdin=subprocess.DEVNULL,
                                            stdout=self.log, stderr=subprocess.STDOUT,
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.check_cancelled()
                if self.process.poll() is not None:
                    self.log.seek(0)
                    raise RuntimeError(self.log.read().decode('utf-8', 'replace')[-5000:])
                try:
                    self.sock = socket.create_connection(('127.0.0.1', port), timeout=0.25)
                    break
                except OSError:
                    cancel.wait(0.1)
            if self.sock is None:
                raise TimeoutError('OpenOCD 连接超时')
        except Exception as exc:
            try:
                detail = self.diagnostic_log()
            except Exception:
                detail = ''
            self.close()
            if detail and detail not in str(exc):
                raise RuntimeError('%s\nOpenOCD 日志：\n%s' % (exc, detail)) from exc
            raise

    def check_cancelled(self):
        if self.cancel.is_set():
            raise RuntimeError('烧录已取消；固件可能未写完，请重新烧录')

    def command(self, command, timeout=120):
        self.check_cancelled()
        # RPC normally returns errors as text. Turn every Tcl error into a tagged response.
        wrapper = 'if {[catch {%s} _rttv_result]} {return "RTTV_ERROR:$_rttv_result"}; return "RTTV_OK:$_rttv_result"' % command
        self.sock.sendall(wrapper.encode('utf-8') + b'\x1a')
        self.sock.settimeout(0.2)
        data = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_cancelled()
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise RuntimeError('OpenOCD 连接中断')
            data.extend(chunk)
            if len(data) > 2 * 1024 * 1024:
                raise RuntimeError('OpenOCD 响应过大')
            if data.endswith(b'\x1a'):
                result = data[:-1].decode('utf-8', 'replace')
                if result.startswith('RTTV_ERROR:'):
                    raise RuntimeError(result[11:])
                if not result.startswith('RTTV_OK:'):
                    raise RuntimeError('OpenOCD 响应格式错误: ' + result[:200])
                return result[8:]
        raise TimeoutError('OpenOCD 操作超时')

    def sectors(self):
        result = []
        count = int(self.command('llength [flash list]'))
        for bank in range(count):
            self.command('flash probe %d' % bank)
            # Return only our own numeric fields, independent of chip/vendor names.
            fields = self.command('array set _rttv_bank [lindex [flash list] %d]; format "%%d %%d" $_rttv_bank(base) $_rttv_bank(size)' % bank)
            base, size = map(int, fields.split())
            text = self.command('capture "flash info %d sectors"' % bank)
            result.extend(parse_sectors(text, bank, base, size))
        return result

    def diagnostic_log(self):
        if self.log is None:
            return ''
        # The child writes to this file directly; use a separate handle so its
        # output offset is not changed while it is still running.
        with open(self.log.name, 'rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 12000))
            return stream.read().decode('utf-8', 'replace').strip()

    def close(self):
        if self.sock is not None:
            self.sock.close()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.log is not None:
            self.log.close()


def program_image(config, image, cancel, progress, server_factory=OpenOCD):
    validate_image(image, config.get('flash_ranges', []), config.get('protected_ranges', []))
    with tempfile.TemporaryDirectory(prefix='mcu-desk-flash-') as directory:
        server = None
        stage = '连接探针'
        try:
            progress(0, stage)
            server = server_factory(config, directory, cancel)
            stage = '复位并暂停芯片'
            progress(0, stage)
            server.command('reset init')
            server.command('wait_halt 3000', timeout=5)
            stage = '读取 Flash 布局'
            progress(0, stage)
            sectors = plan_sectors(image, server.sectors(), config.get('protected_ranges', []))
            stage = '读取并保留扇区'
            prepared = []
            # Read every affected sector before any erase/write. Preserve holes and unrelated bytes.
            for i, (_, _, start, size) in enumerate(sectors):
                progress(int(20 * i / len(sectors)), '读取并保留扇区 0x%08X' % start)
                path = Path(directory) / ('sector_%08x.bin' % start)
                server.command('dump_image %s 0x%X %d' % (tcl_word(path), start, size))
                original = path.read_bytes()
                if len(original) != size:
                    raise RuntimeError('扇区备份长度不足，未执行擦写')
                replacement = merge_sector(original, start, image.segments)
                path.write_bytes(replacement)
                prepared.append((start, path, original == replacement))
            for i, (start, path, unchanged) in enumerate(prepared):
                stage = '写入并校验扇区 0x%08X' % start
                server.check_cancelled()
                progress(20 + int(75 * i / len(prepared)), '校验已有数据' if unchanged else '烧录扇区 0x%08X' % start)
                if not unchanged:
                    server.command('flash write_image erase %s 0x%X bin' % (tcl_word(path), start))
                server.command('verify_image %s 0x%X bin' % (tcl_word(path), start))
            if config.get('reset_after_flash'):
                stage = '复位并运行'
                server.command('reset run')
            progress(100, '烧录与校验成功' + ('，程序已运行' if config.get('reset_after_flash') else '，芯片保持暂停'))
        except Exception as exc:
            detail = ''
            if server is not None:
                try:
                    detail = server.diagnostic_log()
                except Exception:
                    pass
            message = '%s失败：%s' % (stage, exc)
            if stage == '复位并暂停芯片' and not cancel.is_set():
                message += '\n未执行擦写；请检查芯片配置及复位接线。硬件复位/复位下连接需要连接 NRST。'
            if detail:
                message += '\nOpenOCD 日志：\n' + detail
            raise RuntimeError(message) from exc
        finally:
            if server is not None:
                server.close()
