import time
import ctypes
import os


def validate_windows_library(path):
    """Reject corrupt/wrong-bitness DLLs before entering the Windows loader."""
    with open(path, 'rb') as stream:
        dos = stream.read(64)
        if len(dos) != 64 or dos[:2] != b'MZ':
            raise ValueError('J-Link DLL 文件无效，请选择 SEGGER 安装目录中的 DLL')
        stream.seek(int.from_bytes(dos[60:64], 'little'))
        header = stream.read(24)
    if len(header) != 24 or header[:4] != b'PE\0\0' or not int.from_bytes(header[22:24], 'little') & 0x2000:
        raise ValueError('所选文件不是有效的 Windows DLL')
    expected = 0x8664 if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x14c
    if int.from_bytes(header[4:6], 'little') != expected:
        raise ValueError('J-Link DLL 位数与软件不匹配，请选择 %d 位 DLL' % (ctypes.sizeof(ctypes.c_void_p) * 8))


class JLink(object):
    def __init__(self, dllpath, mode='arm', core='Cortex-M0', speed=4000, serial=None):
        if os.name == 'nt':
            validate_windows_library(dllpath)
        self.jlk = ctypes.cdll.LoadLibrary(dllpath)
        self._bind_api()
        try:
            if self.jlk.JLINKARM_DEVICE_GetIndex(core.encode('ascii')) < 0:
                raise ValueError('J-Link 不支持芯片名称“%s”，请在工程设置的芯片下拉框中重新选择' % core)
            if serial:
                if self.jlk.JLINKARM_EMU_SelectByUSBSN(int(serial)) < 0:
                    raise RuntimeError('指定的 J-Link 序列号不可用')
            self.open(mode, core, speed)
        except Exception:
            self.jlk.JLINKARM_Close()
            raise

    def _bind_api(self):
        u32, ptr, integer = ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int
        signatures = {
            'Open': (ctypes.c_char_p, []), 'Close': (None, []),
            'IsOpen': (integer, []), 'IsConnected': (integer, []), 'Connect': (integer, []),
            'EMU_SelectByUSBSN': (integer, [u32]),
            'DEVICE_GetIndex': (integer, [ctypes.c_char_p]),
            'ExecCommand': (integer, [ctypes.c_char_p, ptr, integer]),
            'TIF_Select': (integer, [integer]), 'SetSpeed': (None, [u32]),
            'ReadMemEx': (integer, [u32, u32, ptr, integer]),
            'WriteMem': (integer, [u32, u32, ptr]),
            'GetRegisterList': (integer, [ptr, integer]),
            'GetRegisterName': (ctypes.c_char_p, [u32]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(self.jlk, 'JLINKARM_' + name)
            fn.restype, fn.argtypes = result, args

    def open(self, mode='arm', core='Cortex-M0', speed=4000):
        self.mode = mode.lower()

        error = self.jlk.JLINKARM_Open()
        if error:
            raise ConnectionError('无法打开 J-Link：' + error.decode(errors='replace'))
        if not self.jlk.JLINKARM_IsOpen():
            raise Exception('No JLink connected')

        if self.mode == 'arm':
            tif = TIF.SWD
        elif self.mode == 'rv':
            tif = TIF.CJTAG
        elif self.mode in ('armj', 'rvj'):
            tif = TIF.JTAG
        else:
            raise Exception('invalid mode value')

        if self.jlk.JLINKARM_TIF_Select(tif) < 0:
            raise ConnectionError('J-Link 不支持所选调试接口')
        self.jlk.JLINKARM_SetSpeed(speed)

        err_buf = ctypes.create_string_buffer(512)
        result = self.jlk.JLINKARM_ExecCommand(f'Device = {core}'.encode('ascii'), err_buf, len(err_buf))
        if result != 0:
            raise ConnectionError('J-Link 芯片配置失败：%s；%s' % (core, err_buf.value.decode(errors='replace')))
        if not self.jlk.JLINKARM_IsConnected():
            if self.jlk.JLINKARM_Connect() < 0:
                raise ConnectionError('J-Link 无法连接目标芯片，请检查芯片型号、供电及 SWD 接线')
        if not self.jlk.JLINKARM_IsConnected():
            raise ConnectionError('J-Link 目标连接未建立')

        self.get_registers()

    def get_registers(self):
        buffer = (ctypes.c_uint32 * 0x4000)()
        n_regs = self.jlk.JLINKARM_GetRegisterList(buffer, 0x4000)
        if not 0 <= n_regs <= len(buffer):
            raise ConnectionError('J-Link 读取寄存器列表失败')

        self.jlk.JLINKARM_GetRegisterName.restype = ctypes.c_char_p

        self.core_regs = {}  # 'name: index' pair
        for index in buffer[:n_regs]:
            raw = self.jlk.JLINKARM_GetRegisterName(index)
            if not raw:
                continue
            name = raw.decode()
            self.core_regs[name] = index

    def write_U8(self, addr, val):
        self.write_mem_U8(addr, int(val).to_bytes(1, 'little'))

    def write_U16(self, addr, val):
        self.write_mem_U8(addr, int(val).to_bytes(2, 'little'))

    def write_U32(self, addr, val):
        self.write_mem_U8(addr, int(val).to_bytes(4, 'little'))

    def write_U64(self, addr, val):
        self.write_mem_U8(addr, int(val).to_bytes(8, 'little'))

    def write_mem_U8(self, addr, data):
        buffer = (ctypes.c_uint8 * len(data))(*data)

        result = self.jlk.JLINKARM_WriteMem(addr, len(data), buffer)
        if result != len(data):
            raise ConnectionError('J-Link 内存写入失败：0x%08X，期望 %d 字节，返回 %d' % (addr, len(data), result))

    def write_mem_U32(self, addr, data):
        buffer = (ctypes.c_uint32 * len(data))(*data)

        self.write_mem_U8(addr, bytes(buffer))  # MCU and PC both little-endian

    def read_mem_U8(self, addr, count):
        return self._read_memory(addr, count, ctypes.c_uint8)

    def read_mem_U16(self, addr, count):
        return self._read_memory(addr, count, ctypes.c_uint16)

    def read_mem_U32(self, addr, count):
        return self._read_memory(addr, count, ctypes.c_uint32)

    def read_mem_U64(self, addr, count):
        return self._read_memory(addr, count, ctypes.c_uint64)

    def _read_memory(self, addr, count, unit):
        buffer = (unit * count)()
        size = ctypes.sizeof(buffer)
        result = self.jlk.JLINKARM_ReadMemEx(addr, size, buffer, min(ctypes.sizeof(unit), 4))
        if result != size:
            raise ConnectionError('J-Link 内存读取失败：0x%08X，期望 %d 字节，返回 %d' % (addr, size, result))
        return buffer[:]

    def read_U32(self, addr):
        return self.read_mem_U32(addr, 1)[0]

    def read_U64(self, addr):
        return self.read_mem_U64(addr, 1)[0]

    def read_reg(self, reg):
        val = self.jlk.JLINKARM_ReadReg(self.core_regs[reg])
        if val < 0:
            val += 1 << 32

        return val
    
    def read_regs(self, rlist):
        regIndex = [self.core_regs[reg] for reg in rlist]
        
        regIndex = (ctypes.c_uint32 * len(regIndex))(*regIndex)
        regValue = (ctypes.c_uint32 * len(regIndex))()

        self.jlk.JLINKARM_ReadRegs(regIndex, regValue, 0, len(regIndex))

        return dict(zip(rlist, regValue[:]))

    def write_reg(self, reg, val):
        self.jlk.JLINKARM_WriteReg(self.core_regs[reg], val)

    def reset(self):
        if self.mode.startswith('arm'):
            self.jlk.JLINKARM_Reset()

        else:
            self.jlk.JLINKARM_Reset()

            self.write_reg('pc', 0)
            self.write_reg('dpc', 0)    # When resuming, PC is updated to value in dpc.
            self.go()

    def reset_and_halt(self):
        if self.mode.startswith('arm'):
            self.resetStopOnReset()
            self.write_reg('xpsr', 0x1000000)   # set thumb bit in case the reset handler points to an ARM address

        else:
            self.jlk.JLINKARM_Reset()

    def halt(self):
        self.jlk.JLINKARM_Halt()

    def step(self):
        self.jlk.JLINKARM_Step()

    def go(self):
        self.jlk.JLINKARM_Go()

    def halted(self):
        return self.jlk.JLINKARM_IsHalted()

    def close(self):
        self.jlk.JLINKARM_Close()

    #####################################################################

    # Debug Halting Control and Status Register
    DHCSR = 0xE000EDF0
    C_DEBUGEN   = (1 <<  0)
    C_HALT      = (1 <<  1)
    C_STEP      = (1 <<  2)
    S_REGRDY    = (1 << 16)
    S_HALT      = (1 << 17)
    S_SLEEP     = (1 << 18)
    S_LOCKUP    = (1 << 19)
    S_RETIRE_ST = (1 << 24)     # 1: At least one instruction retired since last DHCSR read.
    S_RESET_ST  = (1 << 25)     # 1: At least one reset since last DHCSR read.

    # Debug Exception and Monitor Control Register
    DEMCR = 0xE000EDFC
    DEMCR_TRCENA       = (1 << 24)
    DEMCR_VC_HARDERR   = (1 << 10)  # Enable halting debug trap on a HardFault exception.
    DEMCR_VC_CORERESET = (1 <<  0)  # Enable Reset Vector Catch. This causes a Local reset to halt a running system.

    def resetStopOnReset(self):
        ''' perform a reset and stop the core on the reset handler '''
        self.halt()

        demcr = self.read_U32(self.DEMCR)

        self.write_U32(self.DEMCR, demcr | self.DEMCR_VC_CORERESET)

        self.reset()
        self.waitReset()
        while not self.halted():
            time.sleep(0.001)

        self.write_U32(self.DEMCR, demcr)

    def waitReset(self):
        ''' wait for the system to come out of reset '''
        startTime = time.time()
        while time.time() - startTime < 2.0:
            try:
                dhcsr = self.read_U32(self.DHCSR)
                if (dhcsr & self.S_RESET_ST) == 0: break
            except Exception as e:
                time.sleep(0.01)


class TIF:
    JTAG  = 0
    SWD   = 1
    CJTAG = 7
