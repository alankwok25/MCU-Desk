"""Serial probe access in a child process; no Qt objects cross this boundary."""
import queue
import struct
import time
from symbols import decode_value


RTT_READ_LIMIT = 1024
POLL_INTERVAL = 0.02
RING_SIZE = 24


def check_transport_error(error):
    """Address faults are local; a lost probe/socket ends the whole session."""
    from pyocd.core.exceptions import ProbeError, TransferFaultError
    if isinstance(error, (TimeoutError, ConnectionError)) or (isinstance(error, ProbeError) and not isinstance(error, TransferFaultError)):
        raise RuntimeError('探针通信中断，请重新连接：' + str(error)) from error


def open_link(config):
    import xlink

    if config['backend'] == 'jlink':
        import jlink
        return xlink.XLink(jlink.JLink(config['dll'], config['mode'],
                                      config['core'], config['speed'], serial=config.get('jlink_uid')))
    if config['backend'] == 'openocd':
        import openocd
        return xlink.XLink(openocd.OpenOCD(mode=config['mode'], core=config['core'],
                                         speed=config['speed']))

    from pyocd.probe import aggregator
    from pyocd.coresight import dap, ap, cortex_m
    from pyocd.probe.pydapaccess.dap_settings import DAPSettings
    # Older CMSIS-DAP firmware can lose responses with multiple requests in flight.
    # The setting is confined to this acquisition child process.
    DAPSettings.limit_packets = True
    probes = aggregator.DebugProbeAggregator.get_all_connected_probes()
    probe = next((p for p in probes if p.unique_id == config['uid']), None)
    if probe is None:
        raise RuntimeError('未找到所选 DAPLink，请检查连接')
    try:
        probe.open()
        probe.set_clock(config['speed'] * 1000)
        dp = dap.DebugPort(probe, None)
        dp.init()
        dp.power_up_debug()
        mem_ap = ap.AHB_AP(dp, 0)
        mem_ap.init()
        return xlink.XLink(cortex_m.CortexM(None, mem_ap))
    except Exception:
        try:
            probe.close()
        except Exception:
            pass
        raise


class Acquisition:
    def __init__(self, link, config, report=None, stop=None):
        self.link = link
        self.config = config
        self.up_addr = None
        self.down_addr = None
        self.report = report or (lambda message: None)
        self.stop = stop
        self.rtt_address = None
        self.rtt_layout = None
        self.guard_enabled = config.get('rtt_health', False)

    def read(self, address, size, source='内存'):
        parts = []
        chunk_size = 256 if self.config.get('backend') == 'daplink' else max(size, 1)
        for offset in range(0, size, chunk_size):
            if self.stop is not None and self.stop.is_set():
                raise InterruptedError('采集已取消')
            count = min(size - offset, chunk_size)
            self.report('%s：读取 0x%08X，%d 字节' % (source, address + offset, count))
            data = bytes(self.link.read_mem_U8(address + offset, count))
            if len(data) != count:
                raise RuntimeError('设备返回的数据长度不足')
            parts.append(data)
        return b''.join(parts)

    def attach_rtt(self, address):
        header = self.read(address, 24, 'RTT 控制块')
        signature, up_count, down_count = struct.unpack('<16sII', header)
        if signature.rstrip(b'\0') != b'SEGGER RTT':
            raise ValueError('RTT 控制块尚未初始化或地址不正确')
        if not 1 <= up_count <= 256 or not 0 <= down_count <= 256:
            raise ValueError('RTT 通道数量无效')
        up_addr = address + 24
        ring = self.read_ring(up_addr)
        self.up_addr = up_addr
        self.down_addr = up_addr + RING_SIZE * up_count if down_count else None
        self.rtt_address = address
        self.rtt_layout = (up_count, down_count, ring[0], ring[1])
        return address

    def find_rtt(self, stop):
        symbol = self.config.get('rtt_symbol')
        automatic = self.config.get('rtt_auto', True)
        candidate = symbol if symbol is not None else self.config['rtt_address']
        # Briefly tolerate firmware startup, without hiding transport failures.
        for attempt in range(3):
            if stop.is_set():
                raise InterruptedError('连接已取消')
            try:
                return self.attach_rtt(candidate)
            except ValueError:
                if attempt < 2:
                    stop.wait(0.15)
                elif not automatic:
                    raise ValueError('指定 RTT 地址无效：0x%08X；请检查固件，或将 RTT 地址设为自动' % candidate)
        start = self.config.get('rtt_scan_start', self.config['rtt_address'])
        search_size = self.config.get('rtt_scan_size', 64 * 1024)
        deadline = time.monotonic() + 2
        for offset in range(0, search_size, 1024):
            if stop.is_set():
                raise InterruptedError('连接已取消')
            if time.monotonic() >= deadline:
                raise RuntimeError('RTT 搜索超时，请在高级设置中缩小 RAM 扫描范围')
            data = self.read(start + offset, min(1024 + 32, search_size - offset))
            index = data.find(b'SEGGER RTT')
            while index >= 0:
                try:
                    return self.attach_rtt(start + offset + index)
                except ValueError:
                    index = data.find(b'SEGGER RTT', index + 1)
        raise RuntimeError('未找到 RTT 控制块，请检查地址及固件是否已启动 RTT')

    def read_ring(self, address):
        _, buffer, size, wr, rd, flags = struct.unpack('<6I', self.read(address, RING_SIZE, 'RTT 通道描述符'))
        if not buffer or not 1 <= size <= 1024 * 1024 or wr >= size or rd >= size:
            raise ValueError('RTT 缓冲区无效，目标可能已复位，请重新连接')
        return buffer, size, wr, rd, flags

    def read_rtt(self):
        buffer, size, wr, rd, _ = self.read_ring(self.up_addr)
        count = min(wr - rd if rd <= wr else size - rd, RTT_READ_LIMIT)
        if not count:
            return b''
        data = self.read(buffer + rd, count, 'RTT 日志')
        self.report('RTT 读指针：写入 0x%08X' % (self.up_addr + 16))
        expected = (rd + count) % size
        self.link.write_U32(self.up_addr + 16, expected)
        if self.guard_enabled and self.config.get('backend') == 'daplink':
            # Complete deferred transfers and verify the host-owned read pointer.
            self.link.flush()
            actual, = struct.unpack('<I', self.read(self.up_addr + 16, 4, 'RTT 读指针回读'))
            if actual != expected:
                raise RuntimeError('RTT 读指针未生效或目标已复位：期望 %d，读回 %d' % (expected, actual))
        return data

    def health(self):
        if self.up_addr is None:
            raise RuntimeError('RTT 尚未就绪')
        signature, up, down = struct.unpack('<16sII', self.read(self.rtt_address, 24, 'RTT 健康检查'))
        buffer, size, wr, rd, _ = self.read_ring(self.up_addr)
        if signature.rstrip(b'\0') != b'SEGGER RTT' or (up, down, buffer, size) != self.rtt_layout:
            raise RuntimeError('RTT 控制块已变化，需重新定位')
        # Cortex-M status read only; never reset, halt, or resume the target.
        dhcsr, = struct.unpack('<I', self.read(0xE000EDF0, 4, 'CPU 状态检查'))
        return dict(address=self.rtt_address, wr=wr, rd=rd, size=size,
                    cpu='lockup' if dhcsr & (1 << 19) else 'halted' if dhcsr & (1 << 17) else 'unknown',
                    checked=time.monotonic())

    def write_rtt(self, data):
        if self.down_addr is None:
            raise RuntimeError('RTT 没有可用的下行通道')
        buffer, size, wr, rd, _ = self.read_ring(self.down_addr)
        count = min(len(data), (rd - wr - 1) % size)
        first = min(count, size - wr)
        if first:
            self.report('RTT 下行：写入 0x%08X，%d 字节' % (buffer + wr, first))
            self.link.write_mem_U8(buffer + wr, data[:first])
        if count > first:
            self.report('RTT 下行：写入 0x%08X，%d 字节' % (buffer, count - first))
            self.link.write_mem_U8(buffer, data[first:count])
        if count:
            self.report('RTT 写指针：写入 0x%08X' % (self.down_addr + 12))
            self.link.write_U32(self.down_addr + 12, (wr + count) % size)
        return count

    def sample(self):
        # Separate error paths: one invalid variable must not suppress RTT or other variables.
        log, values, errors = b'', [], {}
        if self.up_addr is not None:
            try:
                log = self.read_rtt()
            except Exception as exc:
                check_transport_error(exc)
                errors['RTT'] = str(exc)
        for row, name, address, size, fmt in self.config['variables']:
            if self.stop is not None and self.stop.is_set():
                break
            try:
                value = decode_value(self.read(address, size, name), fmt, self.config['byte_order'])
            except Exception as exc:
                check_transport_error(exc)
                value = None
                errors[name] = str(exc)
            values.append((row, value))
        return log, values, errors


def run_session(config, output, commands, stop):
    link = None
    operation = '打开调试器'
    last_progress = 0

    def report(message):
        nonlocal operation, last_progress
        operation = message
        now = time.monotonic()
        if config.get('report_progress') and now - last_progress >= 0.25:
            try:
                output.put_nowait(('progress', message))
                last_progress = now
            except queue.Full:
                pass

    try:
        link = open_link(config)
        sampler = Acquisition(link, config, report, stop)
        warning = None
        rtt_address = None
        if config['rtt_enabled']:
            try:
                rtt_address = sampler.find_rtt(stop)
                if config.get('rtt_symbol') is not None and rtt_address != config['rtt_symbol']:
                    warning = 'RTT 已通过 RAM 搜索定位，地址与 ELF 不同；监控变量前请核对 ELF 与板上固件是否一致'
            except Exception as exc:
                check_transport_error(exc)
                if not config['variables']:
                    raise
                warning = str(exc)
        if stop.is_set():
            return
        output.put(('connected', {'rtt_address': rtt_address,
                                  'can_send': sampler.down_addr is not None,
                                  'warning': warning}), timeout=1)
        dropped = 0
        next_health = 0
        rtt_failures = 0
        while not stop.is_set():
            started = time.monotonic()
            send_error = None
            try:
                data = commands.get_nowait()
            except queue.Empty:
                pass
            else:
                try:
                    count = sampler.write_rtt(data)
                    if count != len(data):
                        send_error = 'RTT 发送缓冲区已满，已发送 %d/%d 字节' % (count, len(data))
                except Exception as exc:
                    check_transport_error(exc)
                    send_error = str(exc)
            log, values, errors = sampler.sample()
            health = None
            fatal = None
            if config.get('rtt_health') and config['rtt_enabled']:
                rtt_failures = rtt_failures + 1 if 'RTT' in errors else 0
                if rtt_failures >= 3:
                    fatal = 'RTT 连续读取失败：' + errors['RTT']
                if started >= next_health:
                    next_health = started + 1
                    try:
                        health = sampler.health()
                    except Exception as exc:
                        errors['RTT 健康检查'] = str(exc)
                        fatal = 'RTT 健康检查失败：' + str(exc)
            if send_error:
                errors['发送'] = send_error
            packet = ('sample', {'log': log, 'values': values, 'errors': errors,
                                  'time': started, 'dropped': dropped})
            if health is not None:
                packet[1]['health'] = health
            try:
                output.put(packet, timeout=0.05)
                dropped = 0
            except queue.Full:
                dropped += 1
            if fatal:
                raise RuntimeError(fatal)
            stop.wait(max(0, POLL_INTERVAL - (time.monotonic() - started)))
    except Exception as exc:
        try:
            output.put(('error', operation + '；' + str(exc)), timeout=1)
        except queue.Full:
            pass
    finally:
        if link is not None:
            try:
                link.close()
            except Exception:
                pass
