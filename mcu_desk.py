#! python3
import os
import re
import sys
import datetime
import collections
import configparser
import codecs
import csv
import math
import multiprocessing
import queue
import time

from acquisition import run_session
from rtt_colors import RttColorDecoder

from PyQt5 import QtCore, QtGui, QtWidgets, uic
from PyQt5.QtCore import pyqtSlot, Qt
from PyQt5.QtWidgets import QApplication, QWidget, QDialog, QFileDialog, QTableWidgetItem
from PyQt5.QtChart import QChart, QChartView, QLineSeries

os.environ['PATH'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'libusb-1.0.24/MinGW64/dll') + os.pathsep + os.environ['PATH']


Variable = collections.namedtuple('Variable', 'name addr size')                 # variable from *.elf file
Valuable = collections.namedtuple('Valuable', 'name addr size typ fmt show')    # variable to read and display

zero_if = lambda i: 0 if i == -1 else i

class McuDeskBase(QWidget):
    def __init__(self, parent=None):
        super(McuDeskBase, self).__init__(parent)

        uic.loadUi('mcu_desk.ui', self)

        self.hWidget2.setVisible(False)

        self.tblVar.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)

        self.Vars = {}  # {name: Variable}
        self.Vals = {}  # {row:  Valuable}
        self.elffile = None
        self.rtt_symbol = None
        self.byte_order = '<'
        self.worker = None
        self.can_send = False
        self.session_state = 'idle'
        self.closing = False
        self.rcvfile = None
        self.varfile = None
        self.active_variables = []
        self.last_errors = {}
        self.decoder = None
        self.decoder_encoding = None
        self.rtt_colors = RttColorDecoder()
        self._retry_pending = False
        self._retry_count = 0
        self._session_config = {}
        self.rtt_health = None
        self.rtt_received = 0
        self.rtt_last_data = None
        self.rtt_fault = ''
        self.wavebuff = b''
        self.plot_dirty = False
        self.variable_mode = False

        self.initSetting()

        self.initQwtPlot()

        self.tblVar.setColumnCount(6)
        self.tblVar.setHorizontalHeaderLabels(['变量', '地址', '类型', '采集', '操作', '当前值'])
        self.tblVar.horizontalHeader().setVisible(True)
        self.tblVar.setMaximumHeight(180)
        self.tblVar.setToolTip('连接前双击空白行添加变量，最多 %d 个' % self.N_CURVE)
        self.txtSend.setMaximumHeight(85)
        self.txtMain.document().setMaximumBlockCount(20000)
        self.cmbAddr.setToolTip('“自动”优先使用 ELF 中的 _SEGGER_RTT；无符号时扫描 0x20000000 起的 64 KB。手动地址为扫描起点。')
        self.chkSave.setToolTip('RTT 日志与变量 CSV 分开保存')
        self.linElf.editingFinished.connect(self.load_elf)
        self.chkVars.toggled.connect(self.update_capture_controls)
        self.chkRTT.toggled.connect(self.update_capture_controls)
        self.update_capture_controls()
        self.load_elf()

        self.tmrRTT = QtCore.QTimer()
        self.tmrRTT.setInterval(40)
        self.tmrRTT.timeout.connect(self.on_tmrRTT_timeout)
        self.tmrRTT.start()

        self.tmrRTT_Cnt = 0

    def initSetting(self):
        self.conf = configparser.ConfigParser()
        self.conf.read('setting.ini', encoding='utf-8')

        if not self.conf.has_section('link'):
            self.conf.add_section('link')
            self.conf.set('link', 'mode', 'ARM SWD')
            self.conf.set('link', 'speed', '4 MHz')
            self.conf.set('link', 'jlink', 'path/to/JLink_x64.dll')
            self.conf.set('link', 'select', '')
            self.conf.set('link', 'address', '["0x20000000"]')
            self.conf.set('link', 'variable', '{}')

        self.cmbMode.setCurrentIndex(zero_if(self.cmbMode.findText(self.conf.get('link', 'mode'))))
        self.cmbSpeed.setCurrentIndex(zero_if(self.cmbSpeed.findText(self.conf.get('link', 'speed'))))

        self.cmbDLL.addItem(self.conf.get('link', 'jlink'), 'jlink')
        self.cmbDLL.addItem('OpenOCD Tcl RPC (6666)', 'openocd')
        self.daplink_detect()    # add DAPLink

        self.cmbDLL.setCurrentIndex(zero_if(self.cmbDLL.findText(self.conf.get('link', 'select'))))

        addresses = eval(self.conf.get('link', 'address'))
        old_selection = addresses[0] if addresses else '0x20000000'
        old_elf = '' if re.fullmatch(r'0[xX][0-9a-fA-F]{1,8}', old_selection) or old_selection == '自动' else old_selection
        self.linElf.setText(self.conf.get('link', 'elf', fallback=old_elf))
        self.cmbAddr.addItems([a for a in addresses if re.fullmatch(r'0[xX][0-9a-fA-F]{1,8}', a) or a == '自动'])
        if self.cmbAddr.findText('自动') < 0:
            self.cmbAddr.addItem('自动')
        if old_elf:
            self.cmbAddr.setCurrentText('自动')
        self.chkRTT.setChecked(self.conf.getboolean('link', 'rtt_enabled', fallback=not bool(old_elf)))
        self.chkVars.setChecked(self.conf.getboolean('link', 'vars_enabled', fallback=bool(self.linElf.text())))

        self.Vals = eval(self.conf.get('link', 'variable'))

        if not self.conf.has_section('encode'):
            self.conf.add_section('encode')
            self.conf.set('encode', 'input', 'ASCII')
            self.conf.set('encode', 'output', 'ASCII')
            self.conf.set('encode', 'oenter', r'\r\n')  # output enter (line feed)

            self.conf.add_section('display')
            self.conf.set('display', 'ncurve', '4')     # max curve number supported
            self.conf.set('display', 'npoint', '1000')

            self.conf.add_section('others')
            self.conf.set('others', 'history', '11 22 33 AA BB CC')
            self.conf.set('others', 'savfile', os.path.join(os.getcwd(), 'rtt_data.txt'))

        self.cmbICode.setCurrentIndex(zero_if(self.cmbICode.findText(self.conf.get('encode', 'input'))))
        self.cmbOCode.setCurrentIndex(zero_if(self.cmbOCode.findText(self.conf.get('encode', 'output'))))
        self.cmbEnter.setCurrentIndex(zero_if(self.cmbEnter.findText(self.conf.get('encode', 'oenter'))))

        self.N_CURVE = int(self.conf.get('display', 'ncurve'), 10)
        self.N_POINT = int(self.conf.get('display', 'npoint'), 10)

        self.linFile.setText(self.conf.get('others', 'savfile'))

        self.txtSend.setPlainText(self.conf.get('others', 'history'))

    def initQwtPlot(self):
        self.PlotData  = [[0]*self.N_POINT for i in range(self.N_CURVE)]
        self.plot_rows = []

        self.PlotChart = QChart()

        self.ChartView = QChartView(self.PlotChart)
        self.ChartView.setVisible(False)
        self.displaySplitter = QtWidgets.QSplitter(Qt.Vertical)
        self.vLayout.removeWidget(self.txtMain)
        self.displaySplitter.addWidget(self.ChartView)
        self.displaySplitter.addWidget(self.txtMain)
        self.displaySplitter.setStretchFactor(0, 2)
        self.displaySplitter.setStretchFactor(1, 1)
        self.vLayout.insertWidget(0, self.displaySplitter, 1)

        self.PlotCurve = [QLineSeries() for i in range(self.N_CURVE)]

    def daplink_detect(self):
        try:
            from pyocd.probe import aggregator
            self.daplinks = aggregator.DebugProbeAggregator.get_all_connected_probes()
        except Exception as e:
            self.daplinks = []

        labels = [f'{probe.product_name} ({probe.unique_id})' for probe in self.daplinks]
        if labels != [self.cmbDLL.itemText(i) for i in range(2, self.cmbDLL.count())]:
            selected = self.cmbDLL.currentText()
            for i in range(2, self.cmbDLL.count()):
                self.cmbDLL.removeItem(2)

            for i, label in enumerate(labels):
                self.cmbDLL.addItem(label, i)
            index = self.cmbDLL.findText(selected)
            if index >= 0:
                self.cmbDLL.setCurrentIndex(index)

    def update_capture_controls(self):
        idle = self.session_state == 'idle'
        for widget in (self.cmbDLL, self.btnDLL, self.cmbAddr, self.btnAddr,
                       self.linElf, self.chkRTT, self.chkVars, self.chkSave,
                       self.cmbMode, self.cmbSpeed):
            widget.setEnabled(idle)
        self.tblVar.setVisible(self.chkVars.isChecked())
        self.txtSend.setVisible(self.chkRTT.isChecked())
        self.btnSend.setVisible(self.chkRTT.isChecked())
        self.btnSend.setEnabled(self.session_state == 'active' and self.can_send)
        if self.chkVars.isChecked():
            self.chkWave.setChecked(True)

    def load_elf(self):
        if self.session_state != 'idle':
            return False
        path = self.linElf.text().strip()
        if not path:
            self.elffile = None
            self.rtt_symbol = None
            self.Vars = {}
            self.tblVar.setRowCount(0)
            self.tblVar.insertRow(0)
            return False
        try:
            identity = (path, os.path.getmtime(path))
        except OSError as exc:
            self.elffile = None
            self.rtt_symbol = None
            self.Vars = {}
            self.append_status('ELF: ' + str(exc))
            return False
        if self.elffile != identity:
            if not self.parse_elffile(path):
                self.elffile = None
                return False
            self.elffile = identity
        return True

    def session_config(self, collect_variables=None):
        if collect_variables is None:
            collect_variables = self.chkVars.isChecked()
        elf_ok = self.load_elf()
        variables = []
        if collect_variables:
            if not elf_ok:
                raise ValueError('采集变量需要有效的 ELF / AXF / OUT 文件')
            variables = [(row, val.name, val.addr, val.size, val.fmt)
                         for row, val in self.Vals.items() if val.show]
            if not variables:
                raise ValueError('请先双击变量表的空白行添加需要采集的变量')
        if not self.chkRTT.isChecked() and not variables:
            raise ValueError('请至少启用 RTT 或变量采集')
        text = self.cmbAddr.currentText().strip()
        address, symbol = 0x20000000, None
        if self.chkRTT.isChecked():
            if text == '自动':
                symbol = self.rtt_symbol if elf_ok else None
            elif re.fullmatch(r'0[xX][0-9a-fA-F]{1,8}', text):
                address = int(text, 16)
                if address + 64 * 1024 + 32 > 0x100000000:
                    raise ValueError('RTT 扫描范围超出 32 位地址空间')
            else:
                raise ValueError('RTT 地址应为“自动”或十六进制地址，例如 0x20000000')
        mode = self.cmbMode.currentText()
        mode = mode.replace(' SWD', '').replace(' cJTAG', '').replace(' JTAG', 'J').lower()
        backend = self.cmbDLL.currentData()
        uid = None
        if backend not in ('jlink', 'openocd'):
            if backend is None or backend >= len(self.daplinks):
                raise ValueError('请先选择可用的调试器')
            uid = self.daplinks[backend].unique_id
            backend = 'daplink'
        return dict(backend=backend, uid=uid, dll=self.cmbDLL.currentText(), mode=mode,
                    core='Cortex-M0' if mode.startswith('arm') else 'RISC-V',
                    speed=int(self.cmbSpeed.currentText().split()[0]) * 1000,
                    rtt_enabled=self.chkRTT.isChecked(), rtt_address=address,
                    rtt_symbol=symbol, variables=variables, byte_order=self.byte_order)

    @pyqtSlot()
    def on_btnOpen_clicked(self):
        if self.session_state != 'idle':
            self.stop_session()
            return
        try:
            config = self.session_config()
            self._session_config = config
            self._retry_count = 0
            self._retry_pending = False
            self.rtt_health = None
            self.rtt_received = 0
            self.rtt_last_data = None
            self.rtt_fault = ''
            self.active_variables = config['variables']
            self.variable_mode = bool(self.active_variables)
            self.decoder = None
            self.wavebuff = b''
            self.last_errors = {}
            self.reset_plot()
            for row in range(self.tblVar.rowCount()):
                self.tblVar.setItem(row, 5, QTableWidgetItem('—'))
            if self.chkSave.isChecked():
                base, ext = os.path.splitext(self.linFile.text())
                base += '_' + datetime.datetime.now().strftime('%y%m%d%H%M%S_%f')
                if config['rtt_enabled']:
                    self.rcvfile = open(base + (ext or '.txt'), 'wb')
                if self.active_variables:
                    self.varfile = open(base + '_variables.csv', 'w', newline='', encoding='utf-8-sig')
                    self.varwriter = csv.writer(self.varfile)
                    self.varwriter.writerow(['elapsed_s'] + [v[1] for v in self.active_variables])
            self.started_at = None
            self.start_worker(config)
        except Exception as exc:
            self.append_status('连接失败: ' + str(exc))
            self.finish_session()
            return
        self.session_state = 'connecting'
        self.btnOpen.setText('取消连接')
        self.update_capture_controls()
        self.append_status('正在连接… %s，%d kHz，RTT=%s，变量=%d' %
                            (config['backend'], config['speed'], '开' if config['rtt_enabled'] else '关', len(config['variables'])))

    def start_worker(self, config):
        context = multiprocessing.get_context('spawn')
        self.output = context.Queue(maxsize=8)
        self.commands = context.Queue(maxsize=16)
        self.stop_event = context.Event()
        self.worker = context.Process(target=run_session,
                                      args=(config, self.output, self.commands, self.stop_event), daemon=True)
        self.last_message = time.monotonic()
        self.last_operation = '打开调试器'
        self.can_send = False
        self.terminated = False
        self.decoder = None
        self.wavebuff = b''
        self.rtt_health = None
        self.worker.start()

    def communication_failed(self, reason):
        if self.session_state == 'stopping':
            return
        self.rtt_fault = reason
        retry = self._session_config.get('auto_reconnect', False) and self._retry_count < 3 and not self.closing
        self.stop_session()
        self._retry_pending = retry
        if not retry and self._session_config.get('auto_reconnect'):
            self.append_status('自动恢复次数已用完，请检查探针与目标板；可手动重新连接')

    def stop_session(self):
        self._retry_pending = False
        if self.session_state == 'retrying':
            self.finish_session()
            return
        if self.worker is None or self.session_state == 'stopping':
            return
        self.stop_event.set()
        self.stop_deadline = time.monotonic() + 2
        self.session_state = 'stopping'
        self.btnOpen.setText('正在断开…')
        self.btnOpen.setEnabled(False)
        self.can_send = False
        self.update_capture_controls()

    def close_recordings(self):
        for name in ('rcvfile', 'varfile'):
            handle = getattr(self, name, None)
            if handle is not None:
                try:
                    handle.close()
                except OSError as exc:
                    self.append_status('保存失败: ' + str(exc))
                setattr(self, name, None)

    def finish_session(self):
        if self.worker is not None:
            if self.worker.pid is not None:
                self.worker.join(timeout=0)
            self.worker.close()
            self.worker = None
        for name in ('output', 'commands'):
            channel = getattr(self, name, None)
            if channel is not None:
                channel.cancel_join_thread()
                channel.close()
                setattr(self, name, None)
        if self._retry_pending and not self.closing:
            self._retry_count += 1
            self._retry_at = time.monotonic() + self._retry_count
            self.session_state = 'retrying'
            self.can_send = False
            self.append_status('正在自动恢复 DAPLink（%d/3），保留日志和录制文件；不复位目标' % self._retry_count)
            self.update_capture_controls()
            self.btnOpen.setText('取消自动恢复')
            return
        self.close_recordings()
        self.session_state = 'idle'
        self.can_send = False
        self.btnOpen.setText('打开连接')
        self.btnOpen.setEnabled(True)
        self.update_capture_controls()
        if self.closing:
            QtCore.QTimer.singleShot(0, self.close)

    def on_tmrRTT_timeout(self):
        self.tmrRTT_Cnt += 1
        if self.worker is None:
            if self.session_state == 'retrying':
                if time.monotonic() >= self._retry_at:
                    self._retry_pending = False
                    try:
                        self.start_worker(self._session_config)
                        self.session_state = 'connecting'
                        self.btnOpen.setText('取消自动恢复')
                        self.update_capture_controls()
                    except Exception as exc:
                        self.session_state = 'connecting'
                        self.communication_failed('重新连接失败：' + str(exc))
                        self.finish_session()
                return
            if self.tmrRTT_Cnt % 100 == 1:
                self.daplink_detect()
            return
        for _ in range(32):
            try:
                kind, payload = self.output.get_nowait()
            except queue.Empty:
                break
            self.last_message = time.monotonic()
            if kind == 'connected':
                if self.session_state == 'stopping':
                    continue
                self.session_state = 'active'
                self.rtt_fault = ''
                self.btnOpen.setText('关闭连接')
                self.can_send = payload['can_send']
                self.update_capture_controls()
                if payload['rtt_address'] is not None:
                    self.append_status('RTT 已连接 @ 0x%08X' % payload['rtt_address'])
                if payload['warning']:
                    if payload['rtt_address'] is None:
                        self.append_status('RTT 未启用: ' + payload['warning'] + '；变量采集继续')
                    else:
                        self.append_status(payload['warning'])
                if self.active_variables:
                    self.append_status('变量采集已启动，%d 个变量' % len(self.active_variables))
            elif kind == 'error':
                self.append_status('通信失败: ' + payload)
                self.communication_failed(payload)
            elif kind == 'sample':
                if self.session_state != 'stopping':
                    self.consume_sample(payload)
            elif kind == 'progress':
                self.last_operation = payload
        self.render_plot()
        if not self.worker.is_alive():
            if self.session_state != 'stopping':
                self.append_status('设备连接已结束')
                self.communication_failed('通信进程意外退出')
            self.finish_session()
            return
        now = time.monotonic()
        if self.session_state == 'stopping':
            if now > self.stop_deadline and not self.terminated:
                self.worker.terminate()
                self.terminated = True
                self.append_status('通信进程未及时退出，已终止；可重新连接')
        elif now - self.last_message > (15 if self.session_state == 'connecting' else 5):
            self.append_status('设备通信超时，正在断开；最近操作：' + getattr(self, 'last_operation', '尚未返回状态'))
            self.communication_failed('设备通信超时：' + getattr(self, 'last_operation', '尚未返回状态'))

    def consume_sample(self, sample):
        if sample.get('health') is not None:
            self.rtt_health = sample['health']
        if self.started_at is None:
            self.started_at = sample['time']
        for source, error in sample['errors'].items():
            if self.last_errors.get(source) != error:
                self.append_status(source + ': ' + error)
        self.last_errors = sample['errors']
        if sample['dropped']:
            self.append_status('显示处理不及时，丢弃了 %d 批采样数据' % sample['dropped'])
        log = sample['log']
        self.rtt_received += len(log)
        if log:
            self.rtt_last_data = time.monotonic()
        try:
            if log and self.rcvfile:
                self.rcvfile.write(log)
            if sample['values'] and self.varfile:
                self.varwriter.writerow(['%.6f' % (sample['time'] - self.started_at)] +
                                        [value for _, value in sample['values']])
        except OSError as exc:
            self.append_status('保存失败: ' + str(exc))
            self.close_recordings()
        if log:
            self.append_log(log)
            if not self.variable_mode and self.chkWave.isChecked():
                self.consume_rtt_wave(log)
        for row, value in sample['values']:
            self.tblVar.setItem(row, 5, QTableWidgetItem(str(value) if value is not None else '—'))
            if value is not None and math.isfinite(value):
                self.append_point(row, value)
            elif value is None:
                self.PlotCurve[row].clear()

    def append_status(self, text):
        self.txtMain.append(text)

    def append_log(self, data):
        encoding = self.cmbICode.currentText()
        if self.decoder is None or self.decoder_encoding != encoding:
            self.rtt_colors.reset()
            self.decoder_encoding = encoding
            self.decoder = codecs.getincrementaldecoder('latin-1' if encoding in ('ASCII', 'HEX') else encoding)(errors='replace')
        text = ' '.join('%02X' % x for x in data) + ' ' if encoding == 'HEX' else self.decoder.decode(data)
        runs = [(text, (None, None, False))] if encoding == 'HEX' else self.rtt_colors.feed(text)
        cursor = QtGui.QTextCursor(self.txtMain.document())
        cursor.movePosition(QtGui.QTextCursor.End)
        cursor.beginEditBlock()
        for value, (foreground, background, bold) in runs:
            fmt = QtGui.QTextCharFormat()
            if foreground is not None:
                fmt.setForeground(QtGui.QColor(foreground))
            if background is not None:
                fmt.setBackground(QtGui.QColor(background))
            fmt.setFontWeight(QtGui.QFont.Bold if bold else QtGui.QFont.Normal)
            cursor.insertText(value, fmt)
        cursor.endEditBlock()
        # Also bound a stream containing no newlines. Trim only the oldest text.
        document = self.txtMain.document()
        if document.characterCount() > 2000000:
            trim = QtGui.QTextCursor(document)
            trim.setPosition(0)
            trim.setPosition(document.characterCount() - 1900000, QtGui.QTextCursor.KeepAnchor)
            trim.removeSelectedText()
        self.txtMain.setTextCursor(cursor)
        self.txtMain.ensureCursorVisible()

    def reset_plot(self):
        self.PlotData = [[0] * self.N_POINT for _ in range(self.N_CURVE)]
        self.plot_rows = [v[0] for v in self.active_variables]
        self.configure_plot(self.plot_rows)
        self.plot_dirty = False

    def configure_plot(self, rows):
        for series in self.PlotChart.series():
            self.PlotChart.removeSeries(series)
        self.plot_rows = list(rows)
        names = {v[0]: v[1] for v in self.active_variables}
        for row in self.plot_rows:
            series = self.PlotCurve[row]
            series.clear()
            series.setName(names[row] if self.variable_mode else 'Curve %d' % (row + 1))
            series.setVisible(True)
            self.PlotChart.addSeries(series)
        if self.plot_rows:
            self.PlotChart.createDefaultAxes()

    def append_point(self, row, value):
        self.PlotData[row].pop(0)
        self.PlotData[row].append(value)
        self.plot_dirty = True

    def consume_rtt_wave(self, data):
        self.wavebuff += data
        records = self.wavebuff.split(b',')
        self.wavebuff = records.pop()
        if len(self.wavebuff) > 65536:
            self.wavebuff = b''
        for record in records:
            try:
                values = [(int(x, 16) if self.cmbICode.currentText() == 'HEX' else float(x))
                          for x in record.split()[:self.N_CURVE]]
                if not values or not all(math.isfinite(v) for v in values):
                    continue
            except (ValueError, OverflowError):
                continue
            rows = list(range(len(values)))
            if rows != self.plot_rows:
                self.configure_plot(rows)
            for row, value in enumerate(values):
                self.append_point(row, value)

    def render_plot(self):
        if not self.plot_dirty or not self.chkWave.isChecked() or not self.plot_rows:
            return
        for row in self.plot_rows:
            self.PlotCurve[row].replace([QtCore.QPointF(i, value)
                                        for i, value in enumerate(self.PlotData[row])])
        minimum = min(min(self.PlotData[row]) for row in self.plot_rows)
        maximum = max(max(self.PlotData[row]) for row in self.plot_rows)
        if minimum == maximum:
            margin = max(abs(minimum) * 0.01, 1)
            minimum, maximum = minimum - margin, maximum + margin
        self.PlotChart.axisY().setRange(minimum, maximum)
        self.PlotChart.axisX().setRange(0, self.N_POINT)
        self.plot_dirty = False

    @pyqtSlot()
    def on_btnSend_clicked(self):
        if self.session_state != 'active' or not self.can_send:
            return
        text = self.txtSend.toPlainText()
        try:
            if self.cmbOCode.currentText() == 'HEX':
                data = bytes(int(x, 16) for x in text.split())
            else:
                if self.cmbEnter.currentText() == r'\r\n':
                    text = text.replace('\n', '\r\n')
                data = text.encode(self.cmbOCode.currentText())
            if len(data) > 4096:
                raise ValueError('单次发送最多 4096 字节，请分批发送')
            self.commands.put_nowait(data)
        except queue.Full:
            self.append_status('发送队列已满，请稍后重试')
        except (ValueError, UnicodeError) as exc:
            self.append_status('发送失败: ' + str(exc))

    @pyqtSlot()
    def on_btnDLL_clicked(self):
        dllpath, filter = QFileDialog.getOpenFileName(caption='JLink_x64.dll path', filter='动态链接库文件 (*.dll *.so)', directory=self.cmbDLL.itemText(0))
        if dllpath != '':
            self.cmbDLL.setItemText(0, dllpath)

    @pyqtSlot()
    def on_btnAddr_clicked(self):
        elfpath, filter = QFileDialog.getOpenFileName(caption='ELF 文件', filter='elf file (*.elf *.axf *.out)', directory=self.linElf.text())
        if elfpath != '':
            self.linElf.setText(elfpath)
            if self.load_elf():
                self.chkVars.setChecked(True)
                if self.rtt_symbol is not None:
                    self.cmbAddr.setCurrentText('自动')

    @pyqtSlot(int)
    def on_chkSave_stateChanged(self, state):
        self.hWidget2.setVisible(state == Qt.Checked)

    @pyqtSlot()
    def on_btnFile_clicked(self):
        savfile, filter = QFileDialog.getSaveFileName(caption='数据保存文件路径', filter='文本文件 (*.txt)', directory=self.linFile.text())
        if savfile:
            self.linFile.setText(savfile)

    def parse_elffile(self, path):
        self.Vars = {}
        self.rtt_symbol = None
        try:
            from elftools.elf.elffile import ELFFile
            with open(path, 'rb') as stream:
                elffile = ELFFile(stream)
                self.byte_order = '<' if elffile.little_endian else '>'
                symbols = elffile.get_section_by_name('.symtab')
                if symbols is None:
                    raise ValueError('文件没有符号表，请使用未剥离符号的 ELF 文件')
                for sym in symbols.iter_symbols():
                    if sym.entry['st_shndx'] == 'SHN_UNDEF':
                        continue
                    if sym.name == '_SEGGER_RTT':
                        self.rtt_symbol = sym.entry['st_value']
                    if sym.entry['st_info']['type'] == 'STT_OBJECT' and sym.entry['st_size'] in (1, 2, 4, 8):
                        self.Vars[sym.name] = Variable(sym.name, sym.entry['st_value'], sym.entry['st_size'])

        except Exception as e:
            self.Vars = {}
            self.rtt_symbol = None
            self.append_status(f'ELF 解析失败: {e}')
            return False

        else:
            Vals = {row: val for row, val in self.Vals.items() if val.name in self.Vars}
            self.Vals = {i: val for i, val in enumerate(list(Vals.values())[:self.N_CURVE])}

            for row, val in self.Vals.items():
                var = self.Vars[val.name]
                if val.addr != var.addr:
                    self.Vals[row] = self.Vals[row]._replace(addr = var.addr)
                if val.size != var.size:
                    typ, fmt = self.len2type[var.size][0]
                    self.Vals[row] = self.Vals[row]._replace(size = var.size, typ = typ, fmt = fmt)

            self.tblVar_redraw()
            return True

    len2type = {
        1: [('int8',  'b'), ('uint8',  'B')],
        2: [('int16', 'h'), ('uint16', 'H')],
        4: [('int32', 'i'), ('uint32', 'I'), ('float',  'f')],
        8: [('int64', 'q'), ('uint64', 'Q'), ('double', 'd')]
    }

    def tblVar_redraw(self):
        while self.tblVar.rowCount():
            self.tblVar.removeRow(0)

        for series in self.PlotChart.series():
            self.PlotChart.removeSeries(series)

        for row, val in self.Vals.items():
            self.tblVar.insertRow(row)
            self.tblVar_setRow(row, val)

        if self.tblVar.rowCount() < self.N_CURVE:
            self.tblVar.insertRow(self.tblVar.rowCount())

    def tblVar_setRow(self, row: int, val: Valuable):
        self.tblVar.setItem(row, 0, QTableWidgetItem(val.name))
        self.tblVar.setItem(row, 1, QTableWidgetItem(f'{val.addr:08X}'))
        self.tblVar.setItem(row, 2, QTableWidgetItem(val.typ))
        self.tblVar.setItem(row, 3, QTableWidgetItem('启用' if val.show else '停用'))
        self.tblVar.setItem(row, 4, QTableWidgetItem('删除'))
        self.tblVar.setItem(row, 5, QTableWidgetItem('—'))

        self.PlotCurve[row].setName(val.name)
        self.PlotCurve[row].setVisible(val.show)
        if self.PlotCurve[row] not in self.PlotChart.series():
            self.PlotChart.addSeries(self.PlotCurve[row])
            self.PlotChart.createDefaultAxes()

    @pyqtSlot(int, int)
    def on_tblVar_cellDoubleClicked(self, row, column):
        if self.session_state != 'idle': return

        if column < 3:
            if not self.load_elf() or not self.Vars:
                self.append_status('请先选择包含可采集变量的 ELF 文件')
                return
            dlg = VarDialog(self, row)
            if dlg.exec() == QDialog.Accepted:
                var = self.Vars[dlg.cmbName.currentText()]
                typ, fmt = dlg.cmbType.currentText(), dlg.cmbType.currentData()

                self.Vals[row] = Valuable(var.name, var.addr, var.size, typ, fmt, True)

                self.tblVar_setRow(row, self.Vals[row])

                if self.tblVar.rowCount() < self.N_CURVE and row == self.tblVar.rowCount() - 1:
                    self.tblVar.insertRow(self.tblVar.rowCount())

        elif column == 3:
            if self.tblVar.item(row, 3):
                self.Vals[row] = self.Vals[row]._replace(show = not self.Vals[row].show)

                self.tblVar.item(row, 3).setText('启用' if self.Vals[row].show else '停用')

                self.PlotCurve[row].setVisible(self.Vals[row].show)

        elif column == 4:
            if self.tblVar.item(row, 4):
                self.Vals.pop(row)
                self.Vals = {i: val for i, val in enumerate(self.Vals.values())}

                self.tblVar_redraw()

    @pyqtSlot(int)
    def on_chkWave_stateChanged(self, state):
        self.ChartView.setVisible(state == Qt.Checked)
        self.txtMain.setVisible(True)
        self.wavebuff = b''
        self.plot_dirty = True

    @pyqtSlot()
    def on_btnClear_clicked(self):
        self.txtMain.clear()

    def closeEvent(self, evt):
        if self.worker is not None:
            self.closing = True
            self.stop_session()
            evt.ignore()
            return
        self.tmrRTT.stop()
        self.close_recordings()

        self.conf.set('link',   'mode',   self.cmbMode.currentText())
        self.conf.set('link',   'speed',  self.cmbSpeed.currentText())
        self.conf.set('link',   'jlink',  self.cmbDLL.itemText(0))
        self.conf.set('link',   'select', self.cmbDLL.currentText())
        self.conf.set('encode', 'input',  self.cmbICode.currentText())
        self.conf.set('encode', 'output', self.cmbOCode.currentText())
        self.conf.set('encode', 'oenter', self.cmbEnter.currentText())
        self.conf.set('others', 'history', self.txtSend.toPlainText())
        self.conf.set('others', 'savfile', self.linFile.text())

        addrs = [self.cmbAddr.currentText()] + [self.cmbAddr.itemText(i) for i in range(self.cmbAddr.count())]
        self.conf.set('link',   'address', repr(list(collections.OrderedDict.fromkeys(addrs))))   # 保留顺序去重

        self.conf.set('link',   'variable', repr(self.Vals))
        self.conf.set('link',   'elf', self.linElf.text().strip())
        self.conf.set('link',   'rtt_enabled', str(self.chkRTT.isChecked()))
        self.conf.set('link',   'vars_enabled', str(self.chkVars.isChecked()))

        with open('setting.ini', 'w', encoding='utf-8') as stream:
            self.conf.write(stream)



from PyQt5.QtWidgets import QSizePolicy, QDialogButtonBox

class VarDialog(QDialog):
    def __init__(self, parent, row):
        super(VarDialog, self).__init__(parent)

        self.resize(400, 100)
        self.setWindowTitle('VarDialog')

        self.cmbType = QtWidgets.QComboBox(self)
        self.cmbType.setMinimumSize(QtCore.QSize(80, 0))

        self.cmbName = QtWidgets.QComboBox(self)
        self.cmbName.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.cmbName.currentTextChanged.connect(self.on_cmbName_currentTextChanged)

        self.hLayout = QtWidgets.QHBoxLayout()
        self.hLayout.addWidget(QtWidgets.QLabel('变量：', self))
        self.hLayout.addWidget(self.cmbName)
        self.hLayout.addWidget(QtWidgets.QLabel('    ', self))
        self.hLayout.addWidget(QtWidgets.QLabel('类型：', self))
        self.hLayout.addWidget(self.cmbType)

        self.btnBox = QtWidgets.QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.btnBox.accepted.connect(self.accept)
        self.btnBox.rejected.connect(self.reject)

        self.vLayout = QtWidgets.QVBoxLayout(self)
        self.vLayout.addLayout(self.hLayout)
        self.vLayout.addItem(QtWidgets.QSpacerItem(20, 40, QSizePolicy.Minimum, QSizePolicy.Expanding))
        self.vLayout.addWidget(self.btnBox)

        self.cmbName.addItems(parent.Vars.keys())

        if parent.tblVar.item(row, 0):
            self.cmbName.setCurrentText(parent.tblVar.item(row, 0).text())
            self.cmbType.setCurrentText(parent.tblVar.item(row, 2).text())

    @pyqtSlot(str)
    def on_cmbName_currentTextChanged(self, name):
        size = self.parent().Vars[name].size

        self.cmbType.clear()
        for typ, fmt in self.parent().len2type[size]:
            self.cmbType.addItem(typ, fmt)
