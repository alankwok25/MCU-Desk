"""Portable, versioned project settings; saved watches contain no cached addresses."""
import json
from pathlib import Path
import os
import tempfile


PATH_FIELDS = ('elf', 'image', 'openocd', 'target_config', 'interface_config', 'jlink_dll', 'jlink_exe', 'scripts_dir')


def defaults():
    return dict(version=1, name='未保存工程', chip='', elf='', image='', bin_address='',
                backend='daplink', probe_uid='', jlink_uid='', jlink_dll='', jlink_exe='', mode='ARM SWD', speed='1 MHz',
                rtt_enabled=True, vars_enabled=True, rtt_address='自动', rtt_scan_size=65536,
                rtt_scan_start='0x20000000', openocd='', target_config='', interface_config='',
                scripts_dir='', reset_config='none', flash_ranges=[], protected_ranges=[],
                reset_after_flash=False, watches=[])


def load_project(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('不支持的工程配置版本')
    project = defaults()
    project.update({k: v for k, v in data.items() if k in project})
    for key, default in defaults().items():
        if isinstance(default, (str, bool, int)) and type(project[key]) is not type(default):
            raise ValueError('工程设置格式错误: ' + key)
    if project['backend'] not in ('daplink', 'jlink', 'openocd'):
        raise ValueError('不支持的调试器类型')
    if project['mode'] not in ('ARM SWD', 'ARM JTAG') or project['speed'] not in tuple('%d MHz' % n for n in (1, 2, 4, 5, 8, 10, 20, 40, 50, 80)):
        raise ValueError('不支持的连接方式或速度')
    if not 1024 <= project['rtt_scan_size'] <= 4 * 1024 * 1024:
        raise ValueError('RTT 扫描长度无效')
    for key in PATH_FIELDS:
        value = project[key]
        if not isinstance(value, str):
            raise ValueError('工程路径格式错误: ' + key)
        builtin = key in ('target_config', 'interface_config') and value.startswith(('target/', 'interface/', 'board/'))
        if value and not Path(value).is_absolute() and not builtin:
            project[key] = str((path.parent / value).resolve())
    if not isinstance(project['watches'], list) or len(project['watches']) > 64:
        raise ValueError('工程最多保存 64 个监控变量')
    for watch in project['watches']:
        if not isinstance(watch, dict) or not isinstance(watch.get('path'), str):
            raise ValueError('监控变量格式错误')
    for key in ('flash_ranges', 'protected_ranges'):
        validate_ranges(project[key])
    return project


def validate_ranges(ranges):
    if not isinstance(ranges, list):
        raise ValueError('地址范围应为列表')
    for region in ranges:
        if not isinstance(region, (list, tuple)) or len(region) != 2:
            raise ValueError('地址范围格式应为 [起始地址, 结束地址（不含）]')
        start, end = region
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= 0x100000000:
            raise ValueError('地址范围无效')


def save_project(path, project):
    path = Path(path).resolve()
    data = dict(project)
    for key in PATH_FIELDS:
        value = data.get(key, '')
        builtin = key in ('target_config', 'interface_config') and value.startswith(('target/', 'interface/', 'board/'))
        if value and not builtin:
            try:
                data[key] = os.path.relpath(Path(value).resolve(), path.parent).replace('\\', '/')
            except ValueError:
                data[key] = str(Path(value).resolve())
    data['watches'] = [{'path': w['path'], 'plot': bool(w.get('plot', True))} for w in project['watches']]
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = stream.name
        json.dump(data, stream, ensure_ascii=False, indent=2)
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
