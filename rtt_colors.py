"""Incremental ANSI / SEGGER RTT color decoding; no terminal side effects.

SEGGER uses SGR 1/2 for bright/normal foreground and 4/24 for background.
Reference: https://github.com/SEGGERMicro/RTT/blob/main/RTT/SEGGER_RTT.h
"""

PALETTE = ('#000000', '#aa0000', '#00aa00', '#aa5500', '#0000aa', '#aa00aa', '#00aaaa', '#aaaaaa',
           '#555555', '#ff5555', '#55ff55', '#ffff55', '#5555ff', '#ff55ff', '#55ffff', '#ffffff')


def indexed_color(index):
    if index < 16:
        return PALETTE[index]
    if index < 232:
        index -= 16
        levels = (0, 95, 135, 175, 215, 255)
        rgb = (levels[index // 36], levels[index // 6 % 6], levels[index % 6])
    else:
        rgb = (8 + 10 * (index - 232),) * 3
    return '#%02x%02x%02x' % rgb


class RttColorDecoder:
    def __init__(self):
        self.reset()

    def reset(self):
        self.fg = self.bg = None
        self.bright_fg = self.bright_bg = False
        self.state, self.control, self.length = 'text', '', 0

    def style(self):
        def color(value, bright):
            if isinstance(value, int):
                return PALETTE[value + (8 if bright and value < 8 else 0)]
            return value
        return color(self.fg, self.bright_fg), color(self.bg, self.bright_bg), self.bright_fg

    def sgr(self, command):
        if len(command) > 96:
            return
        try:
            codes = [int(part or '0') for part in command.split(';')]
        except ValueError:
            return
        i = 0
        while i < len(codes):
            code = codes[i]
            i += 1
            if code == 0:
                self.fg = self.bg = None
                self.bright_fg = self.bright_bg = False
            elif code == 1:
                self.bright_fg = True
            elif code in (2, 22):
                self.bright_fg = False
            elif code in (4, 24):
                self.bright_bg = code == 4
            elif 30 <= code <= 37:
                self.fg = code - 30
            elif 40 <= code <= 47:
                self.bg = code - 40
            elif 90 <= code <= 97:
                self.fg = code - 90 + 8
            elif 100 <= code <= 107:
                self.bg = code - 100 + 8
            elif code == 39:
                self.fg = None
            elif code == 49:
                self.bg = None
            elif code in (38, 48):
                # Consume extended colors as a unit; RGB components aren't SGR codes.
                if i >= len(codes):
                    return
                mode = codes[i]
                count = 1 if mode == 5 else 3 if mode == 2 else None
                if count is None or i + count >= len(codes):
                    return
                values = codes[i + 1:i + 1 + count]
                i += count + 1
                if all(0 <= value <= 255 for value in values):
                    value = indexed_color(values[0]) if mode == 5 else '#%02x%02x%02x' % tuple(values)
                    if code == 38:
                        self.fg = value
                    else:
                        self.bg = value

    def feed(self, text):
        runs, plain = [], []
        def flush():
            if plain:
                runs.append((''.join(plain), self.style()))
                plain.clear()
        for char in text:
            if self.state == 'text':
                if char == '\x1b':
                    flush()
                    self.state = 'escape'
                elif char >= ' ' or char in '\n\r\t':
                    plain.append(char)
            elif self.state == 'escape':
                self.control, self.length = '', 0
                if char == '[':
                    self.state = 'csi'
                elif char in ']P^_':
                    self.state = 'string'
                elif char != '\x1b':
                    self.state = 'text'
            elif self.state == 'csi':
                if '@' <= char <= '~':
                    if char == 'm':
                        self.sgr(self.control)
                    self.state, self.control = 'text', ''
                elif char == '\x1b':
                    self.state, self.control = 'escape', ''
                elif char in '\r\n':
                    self.state, self.control = 'text', ''
                    plain.append(char)
                elif len(self.control) >= 96:
                    self.state, self.control = 'text', ''
                else:
                    self.control += char
            else:
                # Discard OSC/DCS strings (titles, hyperlinks, etc.) with a bound.
                self.length += 1
                if char == '\x07' or (self.state == 'string_escape' and char == '\\') or self.length >= 2048:
                    self.state = 'text'
                else:
                    self.state = 'string_escape' if char == '\x1b' else 'string'
        flush()
        return runs
