"""Invoke the installed SEGGER Commander; never share a live probe session."""
import locale
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import time



def installation_dirs(config):
    preferred = []
    for key in ('jlink_exe', 'jlink_dll'):
        if config.get(key):
            preferred.append(Path(config[key]).parent)
    found = []
    for env in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)'):
        root = os.environ.get(env)
        if root:
            found.extend((Path(root) / 'SEGGER').glob('JLink*'))
    found.sort(key=lambda p: tuple(int(n) for n in re.findall(r'\d+', p.name)), reverse=True)
    return list(dict.fromkeys(preferred + found))


def find_commander(config):
    explicit = config.get('jlink_exe', '')
    if explicit:
        if not Path(explicit).is_file():
            raise ValueError('J-Link 程序路径不存在，请在工程设置中重新选择 JLink.exe')
        return str(Path(explicit).resolve())
    for folder in installation_dirs(config):
        path = folder / 'JLink.exe'
        if path.is_file():
            return str(path.resolve())
    raise ValueError('未找到 SEGGER JLink.exe，请在工程设置中选择 J-Link 烧录程序')


def find_jlink_dll(config):
    if config.get('jlink_dll'):
        return config['jlink_dll']
    # Do not load a DLL to inspect it: read its PE architecture first.
    machine = 0x8664 if struct.calcsize('P') == 8 else 0x14c
    for folder in installation_dirs(config):
        for name in ('JLink_x64.dll', 'JLinkARM.dll'):
            path = folder / name
            try:
                with path.open('rb') as stream:
                    if stream.read(2) != b'MZ':
                        continue
                    stream.seek(60)
                    offset = int.from_bytes(stream.read(4), 'little')
                    stream.seek(offset)
                    if stream.read(4) == b'PE\0\0' and int.from_bytes(stream.read(2), 'little') == machine:
                        return str(path.resolve())
            except OSError:
                continue
    return ''


def flash_engine(config):
    # Retain the existing sector-level protection policy when a protected range is configured.
    return 'jlink' if config['backend'] == 'jlink' and not config.get('protected_ranges') else 'openocd'


def write_hex_snapshot(image, path):
    def record(address, kind, data):
        raw = bytes([len(data)]) + address.to_bytes(2, 'big') + bytes([kind]) + data
        return ':' + (raw + bytes([-sum(raw) & 255])).hex().upper() + '\n'
    with Path(path).open('w', encoding='ascii', newline='\n') as stream:
        upper = None
        for address, data in image.segments:
            offset = 0
            while offset < len(data):
                current = address + offset
                if current >> 16 != upper:
                    upper = current >> 16
                    stream.write(record(0, 4, upper.to_bytes(2, 'big')))
                count = min(32, len(data) - offset, 0x10000 - (current & 0xffff))
                stream.write(record(current & 0xffff, 0, data[offset:offset + count]))
                offset += count
        stream.write(record(0, 1, b''))


def commander_arguments(config, executable):
    chip = config.get('chip', '').strip()
    if not chip or not re.fullmatch(r'[A-Za-z0-9_().+ /-]+', chip) or chip.startswith('-'):
        raise ValueError('请选择 SEGGER 支持的完整芯片型号')
    serial = config.get('jlink_uid', '').strip()
    if serial and (not serial.isascii() or not serial.isdecimal()):
        raise ValueError('J-Link 序列号应为数字')
    mode = config.get('mode', 'ARM SWD')
    if mode not in ('ARM SWD', 'ARM JTAG'):
        raise ValueError('不支持的 J-Link 接口')
    speed = int(config.get('speed_khz', 1000))
    if not 1 <= speed <= 100000:
        raise ValueError('J-Link 速度无效')
    args = [executable, '-NoGui', '1', '-ExitOnError', '1', '-Device', chip,
            '-If', 'SWD' if mode == 'ARM SWD' else 'JTAG', '-Speed', str(speed),
            '-AutoConnect', '1', '-CommandFile', 'flash.jlink']
    if serial:
        args += ['-SelectEmuBySN', serial]
    if mode == 'ARM JTAG':
        args += ['-JTAGConf', '-1,-1']
    return args


def program_with_jlink(config, image, cancel, progress, process_factory=None, timeout=180):
    from flashing import validate_image
    validate_image(image, config.get('flash_ranges', []), config.get('protected_ranges', []))
    if config.get('protected_ranges'):
        raise ValueError('保护区工程需要使用原有 OpenOCD 扇区保护烧录流程')
    args = commander_arguments(config, find_commander(config))
    def check():
        if cancel.is_set():
            raise RuntimeError('烧录已取消；固件可能未写完，请重新烧录')
    check()
    with tempfile.TemporaryDirectory(prefix='mcu-desk-segger-') as directory:
        write_hex_snapshot(image, Path(directory) / 'firmware.hex')
        # Only fixed ASCII filenames go into Commander syntax; original paths and contents
        # cannot inject commands, and a rebuild cannot change the previewed firmware.
        commands = ['ExitOnError 1', 'r', 'h', 'exec SetVerifyDownload = 1',
                    'exec SetVerifyRAMDownload = 1', 'loadfile firmware.hex']
        if config.get('reset_after_flash'):
            commands += ['r', 'g']
        else:
            commands += ['h']
        commands += ['exit']
        (Path(directory) / 'flash.jlink').write_text('\n'.join(commands) + '\n', encoding='ascii')
        factory = process_factory or subprocess.Popen
        with (Path(directory) / 'commander.log').open('w+b') as output:
            with (Path(directory) / 'commander.log').open('rb') as reader:
                process = None
                pending = b''
                tail = ''
                downloading = False
                download_ok = False
                def drain(final=False):
                    nonlocal pending, tail, downloading, download_ok
                    while True:
                        chunk = reader.read(65536)
                        pending += chunk
                        lines = pending.split(b'\n')
                        pending = lines.pop()
                        if final and not chunk and pending:
                            lines.append(pending)
                            pending = b''
                        for line in lines:
                            text = line.decode(locale.getpreferredencoding(False), 'replace').strip()
                            if text:
                                tail = (tail + '\n' + text)[-6000:]
                                if 'Downloading file' in text or 'J-Link>loadfile' in text:
                                    downloading = True
                                if downloading and text == 'O.K.':
                                    download_ok = True
                                progress(35, text)
                        if not final or not chunk:
                            break
                try:
                    check()
                    progress(0, '调用 SEGGER J-Link，烧录并校验固件')
                    process = factory(args, cwd=directory, stdin=subprocess.DEVNULL, stdout=output,
                                      stderr=subprocess.STDOUT,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    deadline = time.monotonic() + timeout
                    while process.poll() is None:
                        drain()
                        check()
                        if time.monotonic() >= deadline:
                            raise TimeoutError('J-Link 烧录超时；固件可能未写完，请重新烧录')
                        cancel.wait(.1)
                    drain(final=True)
                    check()
                    if process.returncode != 0:
                        raise RuntimeError('J-Link 返回错误 %s：%s' % (process.returncode, tail[-3000:]))
                    # A clean process exit alone must not report a skipped download as success.
                    if not download_ok:
                        raise RuntimeError('未收到 J-Link 下载成功结果，请查看上位机日志')
                    progress(100, 'J-Link 烧录与校验成功')
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
