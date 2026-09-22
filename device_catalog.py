"""Read the installed DLL's device database in a bounded, probe-free child."""
import ctypes as C
import multiprocessing as mp
from pathlib import Path
from time import monotonic

from PyQt5 import QtCore, QtWidgets as W
from PyQt5.QtCore import Qt
from jlink import validate_windows_library
from segger_flash import find_jlink_dll


class MemoryArea(C.Structure):
    _fields_ = [('address', C.c_uint32), ('size', C.c_uint32)]


class DeviceInfo(C.Structure):
    # JLINKARM_DEVICE_INFO ABI; SizeofStruct makes version negotiation explicit.
    _fields_ = [('SizeofStruct', C.c_uint32), ('name', C.c_char_p),
                ('core_id', C.c_uint32), ('flash_address', C.c_uint32),
                ('ram_address', C.c_uint32), ('endian', C.c_char),
                ('flash_size', C.c_uint32), ('ram_size', C.c_uint32),
                ('manufacturer', C.c_char_p), ('flash', MemoryArea * 32),
                ('ram', MemoryArea * 32), ('core', C.c_uint32)]


def read_catalog(path):
    validate_windows_library(path)
    dll = C.cdll.LoadLibrary(path)
    get = dll.JLINKARM_DEVICE_GetInfo
    get.argtypes, get.restype = [C.c_int, C.POINTER(DeviceInfo)], C.c_int
    count = get(-1, None)
    if not 0 < count <= 100000:
        raise ValueError('设备库返回无效数量')
    names = set()
    for index in range(count):
        info = DeviceInfo()
        info.SizeofStruct = C.sizeof(info)
        get(index, C.byref(info))
        if info.name:
            names.add(info.name.decode('utf-8', errors='replace'))
    if not names:
        raise ValueError('设备库为空')
    return sorted(names, key=str.casefold)


def catalog_worker(path, sender):
    try:
        sender.send((read_catalog(path), ''))
    except Exception as exc:
        sender.send(([], str(exc)))
    finally:
        sender.close()


_CACHE = {}


class ChipSelector(W.QComboBox):
    textChanged = QtCore.pyqtSignal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = dict(config)
        self.process = self.receiver = None
        self.loaded = False
        self.setEditable(True)
        self.setInsertPolicy(W.QComboBox.NoInsert)
        self.setMinimumWidth(220)
        self.setSizeAdjustPolicy(W.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMaxVisibleItems(15)
        self.lineEdit().setPlaceholderText('输入型号搜索，或填写自定义型号')
        self.setText(config.get('chip', ''))
        self.currentTextChanged.connect(self.textChanged)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._poll)
        if isinstance(parent, W.QDialog):
            parent.finished.connect(self.stop_loading)

    def showEvent(self, event):
        super().showEvent(event)
        if not self.loaded and self.process is None:
            self._start()

    def hideEvent(self, event):
        self.stop_loading()
        super().hideEvent(event)

    def text(self):
        return self.currentText()

    def setText(self, text):
        self.setEditText(text)

    def _start(self):
        try:
            path = find_jlink_dll(self.config)
            self.cache_key = (path, Path(path).stat().st_mtime_ns)
            if self.cache_key in _CACHE:
                self._populate(_CACHE[self.cache_key])
                return
            self.setToolTip('正在读取本机 SEGGER 芯片列表…')
            ctx = mp.get_context('spawn')
            self.receiver, sender = ctx.Pipe(duplex=False)
            self.process = ctx.Process(target=catalog_worker, args=(path, sender), daemon=True)
            self.process.start()
            sender.close()
            self.deadline = monotonic() + 8
            self.timer.start()
        except Exception as exc:
            self.setToolTip('芯片列表不可用，可手动输入型号：' + str(exc))
            self.stop_loading()

    def _populate(self, names):
        self.loaded = True
        selected = self.text()
        self.blockSignals(True)
        self.clear()
        self.addItems(names)
        self.setText(selected)
        self.blockSignals(False)
        complete = W.QCompleter(self.model(), self)
        complete.setCaseSensitivity(Qt.CaseInsensitive)
        complete.setFilterMode(Qt.MatchContains)
        complete.setCompletionMode(W.QCompleter.PopupCompletion)
        self.setCompleter(complete)
        self.setToolTip('本机 SEGGER 支持 %d 个型号；输入部分名称搜索，也可手动填写' % len(names))

    def _poll(self):
        try:
            if self.receiver.poll():
                names, error = self.receiver.recv()
                if error:
                    raise ValueError(error)
                _CACHE[self.cache_key] = names
                self._populate(names)
                self.stop_loading()
            elif monotonic() >= self.deadline or not self.process.is_alive():
                raise RuntimeError('读取设备库超时或进程已退出')
        except Exception as exc:
            self.setToolTip('芯片列表不可用，可手动输入型号：' + str(exc))
            self.stop_loading()

    def stop_loading(self, *args):
        self.timer.stop()
        if self.process is not None:
            if self.process.pid:
                self.process.join(0.1)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(0.5)
            self.process = None
        if self.receiver is not None:
            self.receiver.close()
            self.receiver = None
