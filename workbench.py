"""Project-oriented UI over the existing isolated acquisition session."""
import configparser
import json
import os
from pathlib import Path
import shutil
import threading
import math
from collections import defaultdict, deque
from time import monotonic
from types import SimpleNamespace

from PyQt5 import QtCore, QtGui, QtWidgets as W
from PyQt5.QtCore import Qt

from mcu_desk import McuDeskBase, Valuable
from symbols import SymbolIndex
from projects import defaults, load_project, save_project, validate_ranges
from dock_workspace import DockWorkspace
from segger_flash import flash_engine, find_commander, find_jlink_dll
from waveform import WaveformView
from device_catalog import ChipSelector
from probe_discovery import ProbeDiscovery


class SymbolLoader(QtCore.QThread):
    def __init__(self, path, parent):
        super().__init__(parent)
        self.path, self.index, self.error = path, None, None

    def run(self):
        try:
            before = Path(self.path).stat()
            self.index = SymbolIndex(self.path)
            after = Path(self.path).stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise RuntimeError('解析时固件文件发生变化，请重新加载')
            self.identity = (str(Path(self.path).resolve()), after.st_mtime_ns, after.st_size)
        except Exception as exc:
            self.error = str(exc)


class FlashJob(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, str)

    def __init__(self, config, image, parent):
        super().__init__(parent)
        self.config, self.image = config, image
        self.cancel = threading.Event()
        self.error = None

    def run(self):
        try:
            if flash_engine(self.config) == 'jlink':
                from segger_flash import program_with_jlink as program
            else:
                from flashing import program_image as program
            program(self.config, self.image, self.cancel, self.progress.emit)
        except Exception as exc:
            self.error = str(exc)


def browse_row(edit, caption, pattern):
    container = W.QWidget()
    layout = W.QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(edit, 1)
    button = W.QPushButton('浏览…')
    button.clicked.connect(lambda: choose_file(edit, caption, pattern))
    layout.addWidget(button)
    return container


def choose_file(edit, caption, pattern):
    path, _ = W.QFileDialog.getOpenFileName(edit, caption, edit.text(), pattern)
    if path:
        edit.setText(path)


def regions_text(regions):
    return '\n'.join('0x%08X - 0x%08X' % (a, b) for a, b in regions)


def parse_regions(text):
    regions = []
    for line in text.splitlines():
        if line.strip():
            parts = line.split('-')
            if len(parts) != 2:
                raise ValueError('每行填写“起始地址 - 结束地址”，结束地址不包含在范围内')
            regions.append([int(p.strip(), 0) for p in parts])
    validate_ranges(regions)
    return regions


class SettingsDialog(W.QDialog):
    def __init__(self, project, parent):
        super().__init__(parent)
        self.setWindowTitle('工程设置')
        self.resize(680, 300)
        self.project = dict(project)
        layout = W.QVBoxLayout(self)
        hint = W.QLabel('选择调试器和芯片即可连接。只看 RTT 时无需 ELF；烧录文件在烧录窗口选择。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        basic = W.QFormLayout()
        self.probe = W.QComboBox()
        self.probe.setPlaceholderText('请选择调试器；DAPLink 未显示时请连接探针并刷新主界面')
        for i in range(parent.cmbDLL.count()):
            self.probe.addItem(parent.cmbDLL.itemText(i), parent.cmbDLL.itemData(i))
        self.probe.setCurrentIndex(parent.cmbDLL.currentIndex())
        basic.addRow('调试器', self.probe)
        layout.addLayout(basic)
        self.advanced_toggle = W.QToolButton()
        self.advanced_toggle.setText('高级设置')
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.RightArrow)
        layout.addWidget(self.advanced_toggle)
        form = W.QFormLayout()
        self.template = W.QComboBox()
        self.template.addItem('自定义 / 保持当前配置', '')
        for name, target in [('STM32F4 系列', 'target/stm32f4x.cfg'), ('STM32F1 系列', 'target/stm32f1x.cfg'),
                             ('STM32G0 系列', 'target/stm32g0x.cfg'), ('nRF52 系列', 'target/nrf52.cfg'),
                             ('RP2040', 'target/rp2040.cfg')]:
            self.template.addItem(name, target)
        form.addRow('常用芯片模板', self.template)
        self.fields = {}
        for key, label in [('name', '工程名称'), ('chip', '芯片型号'), ('elf', '变量文件（可选）'), ('openocd', 'OpenOCD 程序'),
                           ('target_config', '芯片 / 板卡配置'), ('interface_config', '探针配置（可选）'),
                           ('scripts_dir', 'OpenOCD scripts 目录'), ('jlink_dll', 'J-Link DLL（使用 J-Link 时）'),
                           ('jlink_exe', 'J-Link 烧录程序（留空自动查找）'),
                           ('jlink_uid', 'J-Link 序列号（多探针时填写）'),
                           ('rtt_address', 'RTT 地址（可填“自动”）'), ('rtt_scan_start', 'RTT 默认扫描起点')]:
            edit = ChipSelector(project, self) if key == 'chip' else W.QLineEdit(project.get(key, ''))
            self.fields[key] = edit
            dest = basic if key in ('name', 'chip', 'elf') else form
            if key in ('elf', 'openocd', 'target_config', 'interface_config', 'jlink_dll', 'jlink_exe'):
                dest.addRow(label, browse_row(edit, label, '固件符号 (*.elf *.axf *.out)' if key == 'elf' else '所有文件 (*)'))
            else:
                dest.addRow(label, edit)
        self.fields['elf'].setPlaceholderText('监控结构体和变量时选择；只看 RTT 可留空')
        self.fields['jlink_dll'].setPlaceholderText('留空自动查找')
        self.fields['jlink_exe'].setPlaceholderText('留空自动查找')
        self.fields['target_config'].setPlaceholderText('例如 target/stm32f4x.cfg、target/stm32f1x.cfg，或板卡 cfg 路径')
        self.fields['interface_config'].setPlaceholderText('留空时根据所选 DAPLink / J-Link 自动选择')
        self.template.currentIndexChanged.connect(lambda: self.fields['target_config'].setText(self.template.currentData()) if self.template.currentData() else None)
        self.scan = W.QSpinBox()
        self.scan.setRange(1, 4096)
        self.scan.setSuffix(' KB')
        self.scan.setValue(project['rtt_scan_size'] // 1024)
        form.addRow('RTT 扫描长度', self.scan)
        self.reset = W.QComboBox()
        self.reset.addItems(['none', 'srst_only', 'srst_only srst_nogate connect_assert_srst'])
        self.reset.setCurrentText(project['reset_config'])
        form.addRow('复位接线配置', self.reset)
        self.allowed = W.QPlainTextEdit(regions_text(project['flash_ranges']))
        self.protected = W.QPlainTextEdit(regions_text(project['protected_ranges']))
        for edit in (self.allowed, self.protected):
            edit.setMaximumHeight(75)
            edit.setPlaceholderText('每行：0x08000000 - 0x08100000（结束地址不包含）')
        form.addRow('允许写入范围（可选）', self.allowed)
        form.addRow('保护区（可选）', self.protected)
        content = W.QWidget()
        content.setLayout(form)
        scroll = self.advanced = W.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        scroll.hide()
        note = W.QLabel('未填写允许范围时，以芯片实际 Flash 布局为准。保护区与待写入数据共享擦除扇区时会拒绝烧录。\nBootloader / A/B 工程的启动标志、CRC 和固件组合由工程本身决定，请选择正确的烧录文件。')
        note.setWordWrap(True)
        form.addRow(note)
        self.chip_notice = W.QLabel('芯片已更换：请检查高级设置中的芯片配置、Flash 范围和保护区是否适用。')
        self.chip_notice.setWordWrap(True)
        self.chip_notice.hide()
        layout.addWidget(self.chip_notice)
        self.fields['chip'].textChanged.connect(self.chip_changed)
        self.advanced_toggle.toggled.connect(self.toggle_advanced)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.Save | W.QDialogButtonBox.Cancel)
        buttons.button(W.QDialogButtonBox.Save).setText('保存')
        buttons.button(W.QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def toggle_advanced(self, expanded):
        self.advanced.setVisible(expanded)
        self.advanced_toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.resize(self.width(), 680 if expanded else 300)

    def chip_changed(self, chip):
        changed = chip.strip() != self.project.get('chip', '')
        configured = bool(self.project.get('target_config') or self.project.get('flash_ranges') or self.project.get('protected_ranges'))
        self.chip_notice.setVisible(changed and configured)

    def accept(self):
        try:
            self.project.update({key: edit.text().strip() for key, edit in self.fields.items()})
            self.project['flash_ranges'] = parse_regions(self.allowed.toPlainText())
            self.project['protected_ranges'] = parse_regions(self.protected.toPlainText())
            self.project['rtt_scan_size'] = self.scan.value() * 1024
            self.project['reset_config'] = self.reset.currentText()
            start = int(self.project['rtt_scan_start'], 0)
            if not 0 <= start < start + self.project['rtt_scan_size'] <= 0x100000000:
                raise ValueError('RTT 扫描范围无效')
        except ValueError as exc:
            W.QMessageBox.warning(self, '设置有误', str(exc))
            return
        super().accept()


class FlashDialog(W.QDialog):
    def __init__(self, config, parent):
        super().__init__(parent)
        self.setWindowTitle('烧录固件 · 检查写入内容')
        self.resize(760, 540)
        self.config = dict(config)
        self.image = None
        self.native = flash_engine(config) == 'jlink'
        layout = W.QVBoxLayout(self)
        form = W.QFormLayout()
        self.chip = ChipSelector(config, self)
        form.addRow('芯片型号', self.chip)
        self.file = W.QLineEdit(config.get('image', '') or config.get('elf', ''))
        form.addRow('烧录文件', browse_row(self.file, '选择烧录固件', '固件 (*.hex *.elf *.axf *.out *.bin)'))
        self.address = W.QLineEdit(config.get('bin_address', ''))
        self.address.setPlaceholderText('仅 BIN 需要，例如 0x08000000')
        self.address_label = W.QLabel('BIN 起始地址')
        form.addRow(self.address_label, self.address)
        layout.addLayout(form)
        self.summary = W.QPlainTextEdit()
        self.summary.setReadOnly(True)
        layout.addWidget(self.summary)
        self.reset = W.QCheckBox('校验成功后复位并运行')
        self.reset.setChecked(config.get('reset_after_flash', False))
        self.resume = W.QCheckBox('运行后恢复当前 ELF 的监控（请确认 ELF 与新固件一致）')
        self.resume.setEnabled(self.reset.isChecked())
        self.reset.toggled.connect(self.resume.setEnabled)
        layout.addWidget(self.reset)
        layout.addWidget(self.resume)
        note = W.QLabel(('使用 SEGGER J-Link 烧录并校验，无需 OpenOCD 配置。擦除按芯片扇区执行，扇区内其他数据可能受影响。'
                        if self.native else '使用 OpenOCD 读取并保留受影响扇区中的其他字节，再写入并校验。') +
                       '\n烧录期间暂停监控；请核对固件的写入地址，尤其是 Bootloader / A/B 工程。')
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.Cancel)
        self.start = buttons.addButton('开始烧录', W.QDialogButtonBox.AcceptRole)
        self.start.setObjectName('primary')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.file.textChanged.connect(self.preview)
        self.address.textChanged.connect(self.preview)
        self.chip.textChanged.connect(self.preview)
        self.preview()

    def preview(self):
        from flashing import read_image, validate_image
        is_bin = Path(self.file.text().strip()).suffix.lower() == '.bin'
        self.address.setVisible(is_bin)
        self.address_label.setVisible(is_bin)
        try:
            self.config['chip'] = self.chip.text().strip()
            if not self.config['chip']:
                raise ValueError('请填写芯片完整型号，保存后无需重复输入')
            self.image = read_image(self.file.text().strip(), self.address.text().strip())
            validate_image(self.image, self.config['flash_ranges'], self.config['protected_ranges'])
            lines = ['芯片：' + (self.config['chip'] or '未填写'),
                     '烧录工具：' + ('SEGGER J-Link Commander' if self.native else 'OpenOCD · ' + self.config['target_config']),
                     '探针：' + self.config['backend'] + ' ' + self.config.get('probe_uid', ''),
                     '数据量：%d 字节' % self.image.size, '', '写入地址（实际擦除按芯片扇区执行）：']
            lines += ['0x%08X — 0x%08X  (%d 字节)' % (a, a + len(d) - 1, len(d)) for a, d in self.image.segments]
            lines += ['', 'SHA-256：' + self.image.sha256]
            self.summary.setPlainText('\n'.join(lines))
            self.start.setEnabled(True)
        except Exception as exc:
            self.image = None
            self.summary.setPlainText(str(exc))
            self.start.setEnabled(False)

    def accept(self):
        self.preview()
        if self.image is None:
            return
        self.config.update(image=self.file.text().strip(), bin_address=self.address.text().strip(),
                           reset_after_flash=self.reset.isChecked())
        super().accept()


class Workbench(McuDeskBase):
    MAX_WATCHES = 64

    def __init__(self, project_file=None):
        self.ready = False
        self.project = defaults()
        self.project_path = None
        self.index = None
        self.loader = None
        self.flash_job = None
        self.flash_pending = None
        self.watch_items = {}
        self.tree_items = {}
        self.unresolved = []
        self.reconnect_after_flash = False
        self.saved_layout = None
        self.layout_initialized = False
        self.detached_panel = None
        self.host_log_unread = 0
        self.probe_discovery = ProbeDiscovery()
        self.probe_scan_started = False
        self.probe_scan_error = ''
        self.startup_pending = True
        self.pending_elf = ''
        super().__init__()
        self.build_workbench()
        self.ready = True
        state = Path('workbench-state.json')
        try:
            saved = json.loads(state.read_text(encoding='utf-8')) if state.exists() else {}
            self.saved_layout = saved.get('layout')
            previous = project_file or saved.get('project')
            if previous and Path(previous).is_file():
                self.apply_project(load_project(previous), previous, defer_elf=True)
            elif not project_file and saved.get('draft'):
                project = defaults()
                project.update(saved['draft'])
                self.apply_project(project, defer_elf=True)
            else:
                self.apply_project(defaults())
        except Exception as exc:
            self.apply_project(defaults())
            self.append_status('上次工程未能恢复: ' + str(exc))

    def initSetting(self):
        # Keep the legacy window available, but do not import stale addresses or execute its INI expressions.
        self.conf = configparser.ConfigParser()
        self.cmbDLL.addItem('J-Link（在工程设置中选择 DLL）', 'jlink')
        self.cmbDLL.addItem('OpenOCD 已有服务', 'openocd')
        self.daplinks = []
        self.cmbMode.clear()
        self.cmbMode.addItems(['ARM SWD', 'ARM JTAG'])
        self.cmbAddr.addItems(['自动', '0x20000000'])
        self.N_CURVE = self.MAX_WATCHES
        self.N_POINT = 1000
        self.linFile.setText(str(Path.cwd() / 'capture.txt'))

    def build_workbench(self):
        self.setWindowTitle('MCU Desk · 嵌入式调试工作台')
        available = W.QApplication.primaryScreen().availableGeometry()
        self.resize(min(1440, available.width() - 40), min(1000, available.height() - 80))
        # Reuse the existing widgets and acquisition lifecycle; replace their presentation.
        while self.vLayout.count():
            self.vLayout.takeAt(0)
        self.setStyleSheet('''
            QWidget { font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 9pt; color: #24344b; }
            QWidget#McuDeskBase { background: #eef2f7; }
            QGroupBox { background: white; border: 0; margin: 0; padding: 0; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
            QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QTreeWidget, QTableWidget { background: white; border: 1px solid #d7dfeb; border-radius: 2px; padding: 2px; selection-background-color: #dceaff; selection-color: #183557; }
            QPushButton, QToolButton { background: #f7f9fc; border: 1px solid #cdd7e5; border-radius: 2px; padding: 3px 6px; }
            QToolButton:checked { background: #dceaff; border-color: #8eb2e6; }
            QMenuBar { background: #f5f7fa; spacing: 2px; }
            QMenuBar::item { padding: 3px 9px; }
            QMenuBar::item:selected, QMenu::item:selected { background: #dceaff; }
            QMenu { background: white; padding: 3px; }
            QMenu::item { padding: 5px 22px; }
            QPushButton:hover { background: #e8effa; }
            QPushButton:disabled { color: #94a0b1; background: #f0f3f7; }
            QPushButton#primary { background: #2563eb; color: white; border: 0; }
            QPushButton#primary:disabled { background: #a3bce9; color: white; }
            QHeaderView::section { background: #f2f5fa; border: 0; border-bottom: 1px solid #dde4ee; padding: 3px; }
            QSplitter::handle { background: #c8d5e8; border: 1px solid #edf2f8; border-radius: 3px; }
            QSplitter::handle:hover { background: #80aaf0; }
            QDockWidget::title { background: #e4ebf5; padding: 3px; border: 1px solid #d7dfeb; }
            QMainWindow::separator { background: #c8d5e8; width: 4px; height: 4px; }
            QMainWindow::separator:hover { background: #80aaf0; }
        ''')
        self.vLayout.setContentsMargins(4, 0, 4, 3)
        self.vLayout.setSpacing(3)
        self.menu_bar = W.QMenuBar(self)
        self.menu_bar.setNativeMenuBar(False)
        project_menu = self.menu_bar.addMenu('工程(&F)')
        device_menu = self.menu_bar.addMenu('设备(&D)')
        self.panels_menu = self.menu_bar.addMenu('视图(&V)')
        self.wave_menu = self.menu_bar.addMenu('波形(&W)')
        self.project_label = W.QLabel('未保存工程')
        self.project_label.setMaximumWidth(380)
        self.menu_bar.setCornerWidget(self.project_label)
        self.vLayout.setMenuBar(self.menu_bar)
        self.new_button = self.button('新建工程', self.new_project)
        self.open_button = self.button('打开工程', self.open_project)
        self.save_button = self.button('保存工程', self.save_current_project)
        self.settings_button = self.button('工程设置', self.edit_settings)
        for label, callback, shortcut in (
                ('新建工程', self.new_project, ''),
                ('打开工程…', self.open_project, ''),
                ('保存工程', self.save_current_project, ''),
                ('工程设置…', self.edit_settings, '')):
            project_menu.addAction(label, callback)
        project_menu.aboutToShow.connect(lambda: [a.setEnabled(b.isEnabled()) for a, b in
            zip(project_menu.actions(), (self.new_button, self.open_button, self.save_button, self.settings_button))])
        toolbar = W.QHBoxLayout()
        toolbar.setSpacing(4)
        toolbar.addWidget(W.QLabel('探针'))
        self.cmbDLL.setMaximumWidth(260)
        self.cmbDLL.setMinimumWidth(140)
        self.cmbDLL.setSizePolicy(W.QSizePolicy.Preferred, W.QSizePolicy.Fixed)
        toolbar.addWidget(self.cmbDLL)
        self.refresh_button = self.button('刷新', lambda: self.daplink_detect(force=True))
        toolbar.addWidget(self.refresh_button)
        toolbar.addWidget(self.cmbMode)
        toolbar.addWidget(self.cmbSpeed)
        toolbar.addWidget(self.chkRTT)
        self.chkVars.setText('监控变量')
        self.chkVars.setToolTip('列表为空时只接收 RTT；取消勾选可暂时停止变量采集。')
        toolbar.addWidget(self.chkVars)
        toolbar.addStretch(1)
        self.btnOpen.setObjectName('primary')
        toolbar.addWidget(self.btnOpen)
        self.flash_button = self.button('烧录固件…', self.prepare_flash)
        toolbar.addWidget(self.flash_button)
        self.vLayout.addLayout(toolbar)
        connection = self.connection_details = W.QWidget()
        detail_layout = W.QHBoxLayout(connection)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(W.QLabel('变量文件'))
        detail_layout.addWidget(self.linElf, 1)
        self.btnAddr.setText('选择 ELF…')
        self.btnAddr.setMaximumWidth(16777215)
        self.btnAddr.setMinimumWidth(self.btnAddr.sizeHint().width())
        detail_layout.addWidget(self.btnAddr)
        self.chip_label = W.QLabel('芯片：未填写')
        detail_layout.addWidget(self.chip_label)
        self.vLayout.addWidget(connection)
        self.connection_action = device_menu.addAction('变量文件 / 芯片信息')
        self.connection_action.setCheckable(True)
        self.connection_action.toggled.connect(connection.setVisible)
        self.connection_button = W.QToolButton(self)
        self.connection_button.setDefaultAction(self.connection_action)
        self.connection_button.setToolTip('展开或收起变量文件路径和芯片信息。')
        self.connection_button.setSizePolicy(W.QSizePolicy.Fixed, W.QSizePolicy.Fixed)
        toolbar.insertWidget(toolbar.indexOf(self.flash_button), self.connection_button)
        connection.hide()
        device_menu.addAction('选择变量文件…', lambda: self.btnAddr.click())
        device_menu.addAction('工程设置…', lambda: self.settings_button.click())
        self.status = W.QLabel('从“视图”菜单选择需要显示的板块。')
        self.status.setWordWrap(False)
        self.status.setMinimumWidth(0)
        self.status.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Fixed)
        self.status.setFixedHeight(20)
        self.view_actions = []
        preset_menu = self.panels_menu.addMenu('快捷布局')
        for index, label in enumerate(('全部面板', 'RTT + 波形', '仅 RTT', '仅波形', '仅监控', '上位机日志')):
            action = preset_menu.addAction(label, lambda checked=False, i=index: self.select_view(i))
            self.view_actions.append(action)
        self.current_view = 6
        self.panels_menu.addSeparator()
        # Retain an internal button for the existing reset callback and automation.
        self.restore_layout_button = self.button('恢复布局', self.restore_layout)
        explorer = self.explorer_group = W.QGroupBox()
        explorer.setMinimumWidth(220)
        left = W.QVBoxLayout(explorer)
        self.search = W.QLineEdit()
        self.search.setPlaceholderText('搜索变量，或输入 motor.speed / array[3]')
        left.addWidget(self.search)
        self.tree = W.QTreeWidget()
        self.tree.setHeaderLabels(['变量 / 成员', '类型', '当前值'])
        self.tree.setColumnWidth(0, 210)
        self.tree.setColumnWidth(1, 125)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemExpanded.connect(self.expand_node)
        self.tree.itemDoubleClicked.connect(lambda item, column: self.add_selected(False))
        self.search.textChanged.connect(self.filter_tree)
        self.search.returnPressed.connect(lambda: self.add_selected(False))
        left.addWidget(self.tree, 1)
        actions = W.QHBoxLayout()
        self.watch_button = self.button('添加监控', lambda: self.add_selected(False))
        self.plot_button = self.button('添加到波形', lambda: self.add_selected(True))
        actions.addWidget(self.watch_button)
        actions.addWidget(self.plot_button)
        left.addLayout(actions)
        tree_hint = W.QLabel('展开结构体与数组；双击数值成员即可监控。')
        tree_hint.setWordWrap(True)
        left.addWidget(tree_hint)
        watch_group = self.watch_group = W.QGroupBox()
        watch_group.setMinimumHeight(90)
        watch_layout = W.QVBoxLayout(watch_group)
        self.tblVar.setMaximumHeight(16777215)
        self.tblVar.setMinimumHeight(65)
        self.tblVar.verticalHeader().setDefaultSectionSize(24)
        self.tblVar.verticalHeader().setMinimumSectionSize(20)
        self.tblVar.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
        self.tblVar.setSelectionBehavior(W.QAbstractItemView.SelectRows)
        self.tblVar.setSelectionMode(W.QAbstractItemView.ExtendedSelection)
        self.tblVar.setHorizontalHeaderLabels(['变量 / 成员', '地址', '类型', '波形', '操作', '当前值'])
        self.tblVar.setColumnHidden(1, True)
        header = self.tblVar.horizontalHeader()
        header.setSectionResizeMode(W.QHeaderView.Interactive)
        header.setMinimumSectionSize(36)
        header.setSectionResizeMode(0, W.QHeaderView.Stretch)
        header.setStretchLastSection(False)
        header.moveSection(header.visualIndex(5), 1)
        header.moveSection(header.visualIndex(3), 2)
        for column, width in ((5, 80), (2, 64), (3, 40), (4, 52)):
            self.tblVar.setColumnWidth(column, width)
        self.tblVar.setToolTip('勾选“波形”即可绘制曲线。断开连接后，可右键移除或选中后按 Delete；Ctrl / Shift 支持多选。最多 64 项。')
        self.remove_watch_action = W.QAction('移除选中监控', self.tblVar)
        self.remove_watch_action.setShortcut(QtGui.QKeySequence(Qt.Key_Delete))
        self.remove_watch_action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.remove_watch_action.triggered.connect(self.remove_selected_watches)
        self.tblVar.addAction(self.remove_watch_action)
        self.tblVar.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tblVar.customContextMenuRequested.connect(self.watch_context_menu)
        self.tblVar.itemSelectionChanged.connect(self.update_watch_remove_controls)
        watch_layout.addWidget(self.tblVar)
        chart_group = self.chart_group = W.QGroupBox()
        chart_layout = W.QVBoxLayout(chart_group)
        self._legacy_chart_view = self.ChartView
        self._legacy_chart_view.setParent(self)
        self._legacy_chart_view.hide()
        self.ChartView = WaveformView(self)
        self.ChartView.follow_changed.connect(self.wave_follow_changed)
        self.ChartView.add_actions(self.wave_menu)
        chart_layout.addWidget(self.ChartView.toolbar())
        chart_layout.addWidget(self.ChartView)
        self.ChartView.setMinimumHeight(90)
        self.ChartView.show()
        chart_group.setMinimumHeight(130)
        self.PlotChart.setMargins(QtCore.QMargins(4, 4, 4, 4))
        self.PlotChart.legend().setFont(QtGui.QFont('Microsoft YaHei UI', 8))
        rtt_page = self.log_group = W.QWidget()
        log_layout = W.QVBoxLayout(rtt_page)
        log_layout.setContentsMargins(0, 4, 0, 0)
        log_controls = W.QHBoxLayout()
        self.pause_log = W.QCheckBox('暂停滚动')
        log_controls.addWidget(self.pause_log)
        log_controls.addStretch(1)
        self.log_options_button = W.QToolButton()
        self.log_options_button.setText('接收 / 保存设置')
        self.log_options_button.setCheckable(True)
        log_controls.addWidget(self.log_options_button)
        self.send_options_button = W.QToolButton()
        self.send_options_button.setText('发送栏')
        self.send_options_button.setCheckable(True)
        log_controls.addWidget(self.send_options_button)
        log_controls.addWidget(self.btnClear)
        log_layout.addLayout(log_controls)
        log_options = W.QWidget()
        options = W.QHBoxLayout(log_options)
        options.setContentsMargins(0, 0, 0, 0)
        for widget in (self.cmbICode, self.chkSave, self.linFile, self.btnFile):
            options.addWidget(widget)
        log_layout.addWidget(log_options)
        log_options.hide()
        self.log_options_button.toggled.connect(log_options.setVisible)
        log_layout.addWidget(self.txtMain, 1)
        self.txtMain.setMinimumHeight(45)
        self.txtMain.setStyleSheet('QTextEdit { background: #161b22; color: #d6deeb; selection-background-color: #315b88; }')
        self.txtMain.setToolTip('自动显示 ANSI / SEGGER RTT 颜色；HEX 模式显示原始字节。保存文件保留原始控制码。')
        self.rtt_health_label = W.QLabel('未连接')
        self.rtt_health_label.setFixedHeight(20)
        self.rtt_health_label.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Fixed)
        log_layout.addWidget(self.rtt_health_label)
        self.chkSave.setToolTip('连接前勾选以保存原始 RTT 日志。窗口保留最近 20,000 行 / 约 200 万字符，超限仅移除最早内容。')
        self.btnClear.setToolTip('仅手动清空显示，不删除已保存的日志文件')
        send_box = W.QWidget()
        send = W.QHBoxLayout(send_box)
        send.setContentsMargins(0, 0, 0, 0)
        self.txtSend.setPlaceholderText('输入要发送给 RTT 下行通道的内容')
        self.txtSend.setMaximumHeight(55)
        self.btnSend.setMaximumHeight(55)
        send.addWidget(self.txtSend, 1)
        send.addWidget(self.cmbOCode)
        send.addWidget(self.cmbEnter)
        send.addWidget(self.btnSend)
        log_layout.addWidget(send_box)
        send_box.hide()
        self.send_options_button.toggled.connect(send_box.setVisible)
        host_page = W.QWidget()
        host_layout = W.QVBoxLayout(host_page)
        host_layout.setContentsMargins(0, 4, 0, 0)
        host_actions = W.QHBoxLayout()
        host_hint = W.QLabel('连接、解析、烧录与错误信息')
        host_hint.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Preferred)
        host_actions.addWidget(host_hint, 1)
        self.clear_host_button = self.button('清空上位机日志', self.clear_host_log)
        host_actions.addWidget(self.clear_host_button)
        host_layout.addLayout(host_actions)
        self.txtHost = W.QPlainTextEdit()
        self.txtHost.setReadOnly(True)
        self.txtHost.document().setMaximumBlockCount(2000)
        host_layout.addWidget(self.txtHost, 1)
        for panel_layout in (left, watch_layout, chart_layout, log_layout, host_layout):
            panel_layout.setContentsMargins(2, 2, 2, 2)
            panel_layout.setSpacing(2)
        workspace = self.dock_workspace = DockWorkspace([
            ('browser', '变量浏览器', explorer), ('watch', '监控列表', watch_group),
            ('wave', '变量波形', chart_group), ('logs', 'RTT 输出', rtt_page),
            ('host', '上位机日志', host_page)], self.panels_menu, self)
        self.panels = workspace.docks
        self.panel_actions = workspace.actions
        workspace.changed.connect(self.layout_dragged)
        workspace.host_shown.connect(lambda: self.host_log_tab_changed(1))
        self.panels_menu.addSeparator()
        self.panels_menu.addAction('可见面板左右排列', lambda: workspace.arrange(Qt.Horizontal))
        self.panels_menu.addAction('可见面板上下排列', lambda: workspace.arrange(Qt.Vertical))
        self.dock_return_action = self.panels_menu.addAction('收回浮动面板', workspace.dock_floating_panels)
        self.panels_menu.addAction('恢复默认布局', self.restore_layout)
        self.vLayout.addWidget(workspace, 1)
        self.flash_progress = W.QProgressBar()
        self.flash_progress.setVisible(False)
        self.cancel_flash = self.button('取消烧录', self.cancel_programming)
        self.cancel_flash.setVisible(False)
        progress_row = W.QHBoxLayout()
        progress_row.addWidget(self.flash_progress, 1)
        progress_row.addWidget(self.cancel_flash)
        self.vLayout.addLayout(progress_row)
        self.vLayout.addWidget(self.status)
        for widget in (self.cmbAddr, self.lblDLL, self.lblAddr, self.lblElf, self.btnDLL,
                       self.hWidget2, self.chkTime, self.btnSpace, self.displaySplitter, self.chkWave):
            widget.hide()
        self.chkWave.setChecked(True)
        workspace.reset_layout()

    def select_view(self, index):
        if index == 6:
            return
        views = (tuple(self.panels), ('wave', 'logs'), ('logs',), ('wave',), ('watch',), ('host',))
        self.set_visible_panels(views[index])
        for key in views[index]:
            self.panels[key].raise_()
        self.current_view = index

    def set_visible_panels(self, visible):
        self.dock_workspace.set_visible(visible)

    @QtCore.pyqtSlot(int)
    def on_chkWave_stateChanged(self, state):
        if not hasattr(self, 'panels'):
            super().on_chkWave_stateChanged(state)
            return
        self.panels['wave'].setVisible(state == Qt.Checked)

    def append_status(self, text):
        if not hasattr(self, 'txtHost'):
            return super().append_status(text)
        stamp = QtCore.QTime.currentTime().toString('HH:mm:ss')
        self.txtHost.appendPlainText(stamp + '  ' + str(text))
        if not self.txtHost.isVisible():
            self.host_log_unread += 1
            self.panel_actions['host'].setText('上位机日志 (%d)' % self.host_log_unread)

    def host_log_tab_changed(self, index):
        if index == 1:
            self.host_log_unread = 0
            self.panel_actions['host'].setText('上位机日志')

    def clear_host_log(self):
        self.txtHost.clear()
        self.host_log_tab_changed(1)

    def showEvent(self, event):
        super().showEvent(event)
        if not self.layout_initialized:
            self.layout_initialized = True
            QtCore.QTimer.singleShot(0, self.restore_initial_layout)
            QtCore.QTimer.singleShot(100, self.complete_startup)

    def complete_startup(self):
        if self.closing or not self.startup_pending:
            return
        self.startup_pending = False
        path, self.pending_elf = self.pending_elf, ''
        if path:
            self.start_elf_load(path)
        self.daplink_detect()

    def restore_initial_layout(self):
        if not self.dock_workspace.restore_snapshot(self.saved_layout):
            self.restore_layout()
        else:
            self.layout_dragged()

    def restore_layout(self):
        self.dock_workspace.reset_layout()
        self.layout_dragged()

    def layout_dragged(self, *unused):
        self.current_view = 6

    @staticmethod
    def button(text, callback):
        button = W.QPushButton(text)
        button.clicked.connect(callback)
        return button

    def update_capture_controls(self):
        if not self.ready:
            return
        busy = bool(self.pending_elf) or self.loader is not None or self.flash_job is not None or self.flash_pending is not None
        idle = self.session_state == 'idle' and not busy
        for widget in (self.new_button, self.open_button, self.settings_button, self.save_button,
                       self.cmbDLL, self.refresh_button, self.linElf, self.btnAddr,
                       self.cmbMode, self.cmbSpeed, self.chkRTT, self.chkVars, self.chkSave):
            widget.setEnabled(idle)
        self.refresh_button.setEnabled(idle and not self.probe_discovery.running)
        self.btnOpen.setEnabled(not busy and self.session_state != 'stopping')
        self.flash_button.setEnabled(not busy and self.session_state in ('idle', 'active'))
        self.watch_button.setEnabled(idle and self.index is not None)
        self.plot_button.setEnabled(idle and self.index is not None)
        self.update_watch_remove_controls()
        self.btnSend.setEnabled(self.session_state == 'active' and self.can_send and not busy)
        if self.session_state == 'idle':
            self.btnOpen.setText('连接并监控')
        if self.session_state == 'active':
            sources = 'RTT 与变量按顺序采集' if self.variable_mode and self.chkRTT.isChecked() else '变量采集中' if self.variable_mode else '仅接收 RTT，未采集变量'
            self.status.setText('已连接 · ' + sources + '。修改工程或监控项前请先断开连接。')

    def capture_project(self):
        self.project.update(elf=self.linElf.text().strip(), mode=self.cmbMode.currentText(),
                            speed=self.cmbSpeed.currentText(), rtt_enabled=self.chkRTT.isChecked(),
                            vars_enabled=self.chkVars.isChecked(), rtt_address=self.cmbAddr.currentText())
        selected = self.cmbDLL.currentData()
        self.project['backend'] = selected if selected in ('jlink', 'openocd') else 'daplink'
        if selected == 'jlink':
            self.project['probe_uid'] = self.project.get('jlink_uid', '')
        if isinstance(selected, int) and selected < len(self.daplinks):
            self.project['probe_uid'] = self.daplinks[selected].unique_id
        return dict(self.project)

    def daplink_detect(self, force=False):
        if not self.ready or self.startup_pending or self.closing or self.session_state != 'idle':
            return
        if self.flash_job is not None or self.flash_pending is not None or self.probe_discovery.running:
            return
        if not force and self.cmbDLL.currentData() in ('jlink', 'openocd'):
            self.refresh_button.setToolTip('使用 J-Link / OpenOCD 时不自动扫描 USB；需要 DAPLink 时点击刷新。')
            return
        try:
            self.probe_discovery.start()
            self.probe_scan_started = True
            self.refresh_button.setText('检测中…')
            self.refresh_button.setToolTip('正在后台检测 DAPLink；不影响窗口操作。')
            self.update_capture_controls()
        except Exception as exc:
            self.append_status('探针检测启动失败：' + str(exc))

    def poll_probe_discovery(self):
        result = self.probe_discovery.poll()
        if result is None:
            return
        probes, error = result
        self.refresh_button.setText('刷新')
        self.refresh_button.setToolTip(error or '重新检测 USB 调试器')
        if self.session_state == 'idle' and self.flash_job is None and self.flash_pending is None and not self.closing:
            if error:
                if error != self.probe_scan_error:
                    self.append_status(error)
            else:
                self.update_probe_choices(probes)
            self.probe_scan_error = error
        self.update_capture_controls()

    def stop_probe_discovery(self):
        self.probe_discovery.stop()
        self.refresh_button.setText('刷新')
        self.refresh_button.setToolTip('重新检测 USB 调试器')

    def update_probe_choices(self, probes):
        selected = self.cmbDLL.currentData()
        uid = (self.daplinks[selected].unique_id
               if isinstance(selected, int) and selected < len(self.daplinks)
               else self.project.get('probe_uid', ''))
        if isinstance(selected, int):
            self.project['probe_uid'] = uid
        self.daplinks = [SimpleNamespace(unique_id=uid, product_name=name) for uid, name in probes]
        while self.cmbDLL.count() > 2:
            self.cmbDLL.removeItem(self.cmbDLL.count() - 1)
        for probe in self.daplinks:
            self.cmbDLL.addItem('%s (%s)' % (probe.product_name, probe.unique_id), self.cmbDLL.count() - 2)
        if selected not in ('jlink', 'openocd'):
            index = next((i + 2 for i, probe in enumerate(self.daplinks) if probe.unique_id == uid), -1)
            if not uid and len(self.daplinks) == 1:
                index = 2
            self.cmbDLL.setCurrentIndex(index)

    def apply_project(self, project, path=None, defer_elf=False):
        self.pending_elf = project['elf'] if defer_elf else ''
        project['jlink_dll'] = find_jlink_dll(project)
        self.project, self.project_path = project, str(Path(path).resolve()) if path else None
        self.index, self.elffile, self.rtt_symbol = None, None, None
        self.Vars, self.Vals = {}, {}
        self.active_variables = []
        self.variable_mode = False
        self.tree.clear()
        self.tree_items = {}
        self.tblVar.setRowCount(0)
        self.reset_plot()
        self.linElf.setText(project['elf'])
        self.cmbAddr.setCurrentText(project['rtt_address'])
        self.cmbMode.setCurrentText(project['mode'])
        self.cmbSpeed.setCurrentText(project['speed'])
        self.chkRTT.setChecked(project['rtt_enabled'])
        self.chkVars.setChecked(project['vars_enabled'])
        self.cmbDLL.setItemText(0, 'J-Link · SWD / JTAG')
        self.cmbDLL.setItemData(0, project['jlink_dll'] or '未找到 DLL，可在工程设置中手动选择', Qt.ToolTipRole)
        index = 0 if project['backend'] == 'jlink' else 1 if project['backend'] == 'openocd' else -1
        if project['backend'] == 'daplink':
            index = next((i + 2 for i, probe in enumerate(self.daplinks)
                          if probe.unique_id == project['probe_uid']),
                         2 if len(self.daplinks) == 1 and not project['probe_uid'] else -1)
        self.cmbDLL.setCurrentIndex(index)
        self.project_label.setText(project['name'])
        self.chip_label.setText('芯片：' + (project['chip'] or '请在工程设置中填写型号'))
        self.txtMain.clear()
        self.decoder = None
        self.rtt_colors.reset()
        self.clear_host_log()
        self.status.setText('工程已切换。选择 ELF 并添加监控项；烧录文件在“烧录固件”中单独选择。')
        self.update_capture_controls()
        if self.pending_elf:
            self.status.setText('正在恢复工程，窗口显示后将后台解析 ELF…')
        elif project['elf']:
            self.start_elf_load(project['elf'])

    def new_project(self):
        if not self.confirm_project_switch():
            return
        self.apply_project(defaults())

    def confirm_project_switch(self):
        if not self.project.get('watches') and not self.linElf.text():
            return True
        answer = W.QMessageBox.question(self, '切换工程', '是否先保存当前工程配置？',
                                        W.QMessageBox.Save | W.QMessageBox.Discard | W.QMessageBox.Cancel)
        if answer == W.QMessageBox.Cancel:
            return False
        return self.save_current_project() if answer == W.QMessageBox.Save else True

    def open_project(self):
        if not self.confirm_project_switch():
            return
        path, _ = W.QFileDialog.getOpenFileName(self, '打开工程', '', 'MCU Desk 工程 (*.mcudesk.json *.rttview.json);;JSON (*.json)')
        if path:
            try:
                self.apply_project(load_project(path), path)
            except Exception as exc:
                W.QMessageBox.warning(self, '工程加载失败', str(exc))

    def save_current_project(self):
        path = self.project_path
        if not path:
            path, _ = W.QFileDialog.getSaveFileName(self, '保存工程', 'project.mcudesk.json', 'MCU Desk 工程 (*.mcudesk.json)')
        if not path:
            return False
        try:
            save_project(path, self.capture_project())
            self.project_path = str(Path(path).resolve())
            self.status.setText('工程配置已保存：' + self.project_path)
            return True
        except Exception as exc:
            W.QMessageBox.warning(self, '保存失败', str(exc))
            return False

    def edit_settings(self):
        config = self.capture_project()
        if not config['openocd']:
            config['openocd'] = shutil.which('openocd') or ''
        dialog = SettingsDialog(config, self)
        if dialog.exec() == W.QDialog.Accepted:
            old_elf = self.linElf.text().strip()
            self.project.update(dialog.project)
            self.cmbDLL.setCurrentIndex(dialog.probe.currentIndex())
            self.project['jlink_dll'] = find_jlink_dll(self.project)
            self.project_label.setText(self.project['name'])
            self.chip_label.setText('芯片：' + (self.project['chip'] or '未填写'))
            self.cmbDLL.setItemText(0, 'J-Link · SWD / JTAG')
            self.cmbDLL.setItemData(0, self.project['jlink_dll'] or '未找到 DLL', Qt.ToolTipRole)
            self.cmbAddr.setCurrentText(self.project['rtt_address'])
            if self.project['elf'] != old_elf:
                self.linElf.setText(self.project['elf'])
                self.load_elf()
            self.status.setText('工程设置已更新。请保存工程以便下次使用。')

    def load_elf(self):
        if not self.ready or self.session_state != 'idle' or self.loader is not None:
            return False
        path = self.linElf.text().strip()
        if not path:
            self.index, self.elffile, self.rtt_symbol = None, None, None
            self.tree.clear()
            self.tree_items = {}
            self.rebuild_watches()
            return False
        try:
            stat = Path(path).stat()
            identity = (str(Path(path).resolve()), stat.st_mtime_ns, stat.st_size)
            if self.index is not None and identity == self.elffile:
                return True
        except OSError:
            pass
        if path:
            self.start_elf_load(path)
        return False

    def start_elf_load(self, path):
        if self.loader is not None or self.session_state != 'idle':
            return
        # Invalidate before loading, so errors cannot leave old addresses active.
        self.index, self.elffile, self.rtt_symbol = None, None, None
        self.Vals = {}
        self.tree.clear()
        self.tree_items = {}
        self.rebuild_watches()
        self.status.setText('正在解析固件类型信息…')
        self.loader = SymbolLoader(path, self)
        self.elf_started_at = monotonic()
        self.loader.finished.connect(self.elf_loaded)
        self.update_capture_controls()
        self.loader.start()

    def elf_loaded(self):
        loader = self.loader
        self.loader = None
        if loader.error:
            self.status.setText('ELF 加载失败：' + loader.error)
            self.append_status(self.status.text())
        else:
            self.index = loader.index
            self.elffile = loader.identity
            self.byte_order = self.index.byte_order
            self.rtt_symbol = self.index.rtt_address
            self.project['elf'] = loader.path
            self.populate_tree()
            self.rebuild_watches()
            self.status.setText('已加载 %d 个变量。展开结构体，选中成员后添加监控或波形。%s' %
                                (len(self.index.roots), ' 部分旧监控项已失效，请检查列表。' if self.unresolved else ''))
            for warning in self.index.warnings:
                self.append_status(warning)
        loader.deleteLater()
        self.update_capture_controls()
        if self.closing:
            self.close()
        elif self.reconnect_after_flash and self.index:
            self.reconnect_after_flash = False
            self.on_btnOpen_clicked()

    @QtCore.pyqtSlot()
    def on_btnAddr_clicked(self):
        path, _ = W.QFileDialog.getOpenFileName(self, '选择变量固件', self.linElf.text(), 'ELF 文件 (*.elf *.axf *.out)')
        if path:
            self.linElf.setText(path)
            self.start_elf_load(path)

    def populate_tree(self):
        self.tree.clear()
        self.tree_items = {}
        for node in self.index.roots.values():
            self.add_tree_node(self.tree, node)
        self.filter_tree(self.search.text())

    def add_tree_node(self, parent, node):
        label = node.path if isinstance(parent, W.QTreeWidget) else node.path[len(parent.data(0, Qt.UserRole).path):].lstrip('.')
        item = W.QTreeWidgetItem(parent, [label, node.type.name, '—'])
        item.setData(0, Qt.UserRole, node)
        item.setToolTip(0, node.path + '\n0x%08X' % node.address)
        self.tree_items.setdefault(node.path, []).append(item)
        if node.type.kind in ('struct', 'union', 'array'):
            item.setChildIndicatorPolicy(W.QTreeWidgetItem.ShowIndicator)
        elif not node.selectable:
            item.setToolTip(1, '当前类型不可采集；不推测类型或地址。')
            item.setForeground(1, QtGui.QColor('#8995a5'))
        return item

    def expand_node(self, item):
        if item.childCount():
            return
        node = item.data(0, Qt.UserRole)
        if node is None:
            return
        for child in node.children():
            self.add_tree_node(item, child)
        if node.type.kind == 'array' and node.type.count > 256:
            W.QTreeWidgetItem(item, ['仅展开前 256 项；可输入完整下标添加监控'])

    def filter_tree(self, text):
        text = text.lower().strip()
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            path = item.data(0, Qt.UserRole).path.lower()
            item.setHidden(bool(text) and text not in path and not text.startswith(path + '.') and not text.startswith(path + '['))

    def add_selected(self, plot):
        if self.session_state != 'idle' or self.index is None or self.flash_job is not None:
            return
        node = None
        selected = self.tree.currentItem().data(0, Qt.UserRole) if self.tree.currentItem() else None
        query = self.search.text().strip()
        if query:
            try:
                node = self.index.resolve(query)
                # Searching for a structure must still allow selecting one of its members.
                if selected and (selected.path.startswith(node.path + '.') or selected.path.startswith(node.path + '[')):
                    node = selected
            except KeyError:
                pass
        if node is None:
            node = selected
        if node is None:
            self.status.setText('请先选中左侧变量，或输入完整的变量成员路径。')
            return
        pending = [node]
        existing = {w['path']: w for w in self.project['watches']}
        added = 0
        while pending and len(existing) < self.MAX_WATCHES:
            current = pending.pop(0)
            if current.selectable:
                if current.path in existing:
                    existing[current.path]['plot'] |= plot
                else:
                    watch = {'path': current.path, 'plot': plot}
                    self.project['watches'].append(watch)
                    existing[current.path] = watch
                    added += 1
            else:
                pending[0:0] = current.children(limit=self.MAX_WATCHES)
        self.rebuild_watches()
        self.status.setText('新增 %d 个监控项。%s' % (added, '已达到 64 项上限，请按需选择成员。' if pending else '点击“连接并监控”开始。'))

    def rebuild_watches(self):
        if not self.ready:
            return
        self.Vals = {}
        self.unresolved = []
        watches = self.project['watches']
        self.tblVar.setRowCount(len(watches))
        self.watch_items = {}
        for row, watch in enumerate(watches):
            path = watch['path']
            node = None
            try:
                node = self.index.resolve(path) if self.index else None
            except KeyError:
                pass
            valid = node is not None and node.selectable
            if valid:
                self.Vals[row] = Valuable(path, node.address, node.type.size, node.type.name, node.type.fmt, True)
                self.watch_items[row] = node
            else:
                self.unresolved.append(path)
            for column, text in [(0, path), (1, '%08X' % node.address if valid else '—'),
                                 (2, node.type.name if valid else '无法解析'), (5, '—' if valid else '未找到 / 不支持')]:
                cell = W.QTableWidgetItem(text)
                cell.setToolTip(text)
                self.tblVar.setItem(row, column, cell)
            check = W.QCheckBox()
            check.setChecked(watch.get('plot', False))
            check.toggled.connect(lambda enabled, w=watch: self.set_watch_plot(w, enabled))
            self.tblVar.setCellWidget(row, 3, check)
            remove = self.button('移除', lambda checked=False, p=path: self.remove_watch(p))
            remove.setStyleSheet('QPushButton { padding: 0 2px; min-width: 0; min-height: 0; }')
            self.tblVar.setCellWidget(row, 4, remove)
        self.active_variables = [(r, v.name, v.addr, v.size, v.fmt) for r, v in self.Vals.items()]
        self.variable_mode = bool(self.active_variables)
        self.reset_plot()
        self.update_watch_remove_controls()

    def set_watch_plot(self, watch, enabled):
        watch['plot'] = enabled
        self.reset_plot()

    def remove_watch(self, path):
        self.remove_watch_paths({path})

    def watch_remove_block_reason(self):
        if self.session_state != 'idle':
            return '请先断开连接，再移除监控项。'
        if self.pending_elf or self.loader is not None or self.flash_job is not None or self.flash_pending is not None:
            return '请等待变量解析或烧录结束，再移除监控项。'
        return ''

    def update_watch_remove_controls(self):
        reason = self.watch_remove_block_reason()
        self.remove_watch_action.setEnabled(not reason and bool(self.tblVar.selectionModel().selectedRows()))
        for row in range(self.tblVar.rowCount()):
            button = self.tblVar.cellWidget(row, 4)
            if button is not None:
                button.setEnabled(not reason)
                button.setToolTip(reason or '移除此监控项；也可选中多行后按 Delete。')

    def watch_context_menu(self, position):
        item = self.tblVar.itemAt(position)
        if item is None:
            return
        if not item.isSelected():
            self.tblVar.selectRow(item.row())
        self.update_watch_remove_controls()
        menu = W.QMenu(self.tblVar)
        menu.addAction(self.remove_watch_action)
        reason = self.watch_remove_block_reason()
        if reason:
            menu.addAction(reason).setEnabled(False)
        menu.exec_(self.tblVar.viewport().mapToGlobal(position))

    def remove_selected_watches(self):
        paths = {self.tblVar.item(index.row(), 0).text()
                 for index in self.tblVar.selectionModel().selectedRows()}
        self.remove_watch_paths(paths)

    def remove_watch_paths(self, paths):
        reason = self.watch_remove_block_reason()
        if reason:
            self.status.setText(reason)
            return
        if not paths:
            return
        before = len(self.project['watches'])
        self.project['watches'] = [w for w in self.project['watches'] if w['path'] not in paths]
        self.rebuild_watches()
        self.status.setText('已移除 %d 个监控项。' % (before - len(self.project['watches'])))

    @QtCore.pyqtSlot(int, int)
    def on_tblVar_cellDoubleClicked(self, row, column):
        pass

    def reset_plot(self):
        self.PlotData = [[0] * self.N_POINT for _ in range(self.N_CURVE)]
        self.wave_history = defaultdict(lambda: deque(maxlen=self.N_POINT))
        self.wave_rtt_index = -1
        self.wave_sample_time = 0.
        self._wave_last_render = 0.
        rows = [r for r, *_ in self.active_variables if r < len(self.project['watches']) and self.project['watches'][r].get('plot')]
        self.configure_plot(rows)
        if isinstance(self.ChartView, WaveformView):
            self.ChartView.reset_data()
        self.plot_dirty = True

    def append_point(self, row, value):
        super().append_point(row, value)
        if self.variable_mode:
            x = self.wave_sample_time
        else:
            if row == 0:
                self.wave_rtt_index += 1
            x = self.wave_rtt_index
        self.wave_history[row].append((x, value))

    def wave_follow_changed(self, enabled):
        if enabled:
            self._wave_last_render = 0.
            self.plot_dirty = True
            self.render_plot()

    def render_plot(self):
        if not isinstance(self.ChartView, WaveformView):
            return super().render_plot()
        if not self.plot_dirty or not self.ChartView.follow:
            return
        now = monotonic()
        if self.worker is not None and now - self._wave_last_render < max(.1, len(self.plot_rows) * .005):
            return
        names = {row: value.name for row, value in self.Vals.items()}
        data = {row: tuple(self.wave_history[row]) for row in self.plot_rows}
        labels = {row: names.get(row, 'Curve %d' % (row + 1)) for row in self.plot_rows}
        self.ChartView.set_live_data(data, labels,
            '采集时间 (s)' if self.variable_mode else 'RTT 采样序号')
        self.plot_dirty = False
        self._wave_last_render = now

    def configure_plot(self, rows):
        super().configure_plot(rows)
        if rows:
            for axis in (self.PlotChart.axisX(), self.PlotChart.axisY()):
                axis.setLabelsFont(QtGui.QFont('Microsoft YaHei UI', 8))
                axis.setTickCount(5)
            self.PlotChart.axisX().setLabelFormat('%.0f')
            self.PlotChart.axisY().setLabelFormat('%.3g')
            self.PlotChart.axisY().setTickCount(3)

    def consume_sample(self, sample):
        # Use the proven recording/error handling, with current values also mirrored into the tree.
        self.wave_sample_time = sample['time'] - (self.started_at if self.started_at is not None else sample['time'])
        super().consume_sample(sample)
        for row, value in sample['values']:
            if value is None or not math.isfinite(value):
                self.wave_history[row].append((self.wave_sample_time, math.nan))
                self.plot_dirty = True
            node = self.watch_items.get(row)
            if node is None:
                continue
            text = '—' if value is None else ('%.8g' % value if isinstance(value, float) else str(value))
            if value is not None and node.type.kind == 'pointer':
                text = '0x%X' % value
            if value in node.type.enums:
                text = node.type.enums[value] + ' (%s)' % value
            self.tblVar.item(row, 5).setText(text)
            self.tblVar.item(row, 5).setToolTip(str(value))
            for item in self.tree_items.get(node.path, []):
                item.setText(2, text)

    def append_log(self, data):
        scrollbar = self.txtMain.verticalScrollBar()
        position = scrollbar.value()
        super().append_log(data)
        if self.pause_log.isChecked():
            scrollbar.setValue(position)

    def on_tmrRTT_timeout(self):
        self.poll_probe_discovery()
        if self.loader is not None and not self.closing:
            self.status.setText('正在后台解析 ELF · 已用 %d 秒；完成后可连接，当前可调整面板布局。' % (monotonic() - self.elf_started_at))
        super().on_tmrRTT_timeout()
        self.refresh_rtt_status()

    def refresh_rtt_status(self):
        if not hasattr(self, 'rtt_health_label'):
            return
        now = monotonic()
        health = self.rtt_health
        color = '#526176'
        if self.session_state == 'idle':
            text = '通信异常 · 已停止，详情见上位机日志' if self.rtt_fault else '未连接'
            if self.rtt_fault:
                color = '#b42318'
        elif self.session_state == 'retrying' or (self.session_state == 'stopping' and self._retry_pending):
            text, color = '通信异常 · 正在自动恢复（%d/3）' % max(1, self._retry_count), '#b45309'
        elif self.session_state == 'connecting':
            text = '重新连接中（%d/3）' % self._retry_count if self._retry_count else '正在连接…'
        elif self.session_state == 'stopping':
            text = '正在断开…'
        elif not self.chkRTT.isChecked():
            text = 'RTT 未启用'
        elif health and now - health['checked'] <= 3:
            cpu = health.get('cpu')
            if cpu in ('halted', 'lockup'):
                text, color = ('CPU 已暂停' if cpu == 'halted' else 'CPU Lockup') + ' · 内存可读', '#b45309'
            elif self.rtt_last_data is not None and now - self.rtt_last_data < 2:
                text, color = '正在接收日志 · 内存可读', '#187440'
            else:
                text = '内存可读 · ' + ('尚未收到日志' if self.rtt_last_data is None else '%d 秒无新日志' % (now - self.rtt_last_data))
        elif self._session_config.get('rtt_health'):
            text, color = '通信状态待确认 · 等待健康检查', '#b45309'
        else:
            text = '连接已建立 · ' + ('正在接收' if self.rtt_last_data and now - self.rtt_last_data < 2 else '暂未收到新日志')
        text += ' · %.1f KB' % (self.rtt_received / 1024)
        if self.rtt_health_label.text() != text:
            self.rtt_health_label.setText(text)
            self.rtt_health_label.setStyleSheet('color: %s' % color)
        detail = text + '\n内存可读仅表示调试链路可访问目标 RAM，不保证业务任务正常；确认业务存活需要固件心跳。'
        if health:
            detail += '\nRTT 0x%08X；读指针 %d；写指针 %d；缓冲区 %d 字节' % (health['address'], health['rd'], health['wr'], health['size'])
        detail += '\n窗口仅保留最近 20,000 行 / 约 200 万字符。连接前在“接收 / 保存设置”勾选保存可保留接收到的原始日志。'
        if self.rtt_fault:
            detail += '\n' + self.rtt_fault
        self.rtt_health_label.setToolTip(detail)

    def session_config(self):
        if self.pending_elf or self.loader is not None:
            raise ValueError('正在解析 ELF，请等待变量信息就绪。')
        self.stop_probe_discovery()
        collect_variables = self.chkVars.isChecked() and bool(self.project['watches'])
        if self.chkVars.isChecked() and not self.project['watches'] and not self.chkRTT.isChecked():
            raise ValueError('请展开左侧变量，选中成员后点击“添加监控”或“添加到波形”')
        config = super().session_config(collect_variables=collect_variables)
        config['report_progress'] = True
        config['rtt_health'] = config['rtt_enabled'] and config['backend'] in ('daplink', 'jlink') and config['mode'] in ('arm', 'armj')
        config['auto_reconnect'] = config['backend'] == 'daplink' and config['rtt_enabled']
        if self.unresolved and collect_variables:
            raise ValueError('部分监控项已失效，请移除或重新选择：' + ', '.join(self.unresolved[:4]))
        project = self.capture_project()
        config['rtt_scan_size'] = project['rtt_scan_size']
        config['rtt_auto'] = self.cmbAddr.currentText() == '自动'
        config['rtt_scan_start'] = int(project['rtt_scan_start'], 0)
        if config['backend'] == 'daplink' and config['mode'] != 'arm':
            raise ValueError('DAPLink 直接采集请使用 ARM SWD；其他连接方式可使用已有 OpenOCD 服务')
        if self.cmbAddr.currentText() == '自动' and self.rtt_symbol is None:
            config['rtt_address'] = int(project['rtt_scan_start'], 0)
        if config['rtt_address'] + config['rtt_scan_size'] > 0x100000000:
            raise ValueError('RTT 扫描范围超出地址空间')
        if config['backend'] == 'jlink':
            project['jlink_dll'] = find_jlink_dll(project)
            if not project['jlink_dll'] or not Path(project['jlink_dll']).is_file():
                raise ValueError('请在工程设置中选择正确的 J-Link DLL')
            config['dll'] = project['jlink_dll']
            config['core'] = project['chip'] or 'Cortex-M4'
            config['jlink_uid'] = project.get('jlink_uid', '')
        return config

    def prepare_flash(self):
        self.stop_probe_discovery()
        config = self.capture_project()
        native = flash_engine(config) == 'jlink'
        if native:
            try:
                config['jlink_exe'] = find_commander(config)
            except ValueError as exc:
                self.status.setText(str(exc))
                self.append_status(str(exc))
                self.edit_settings() if self.session_state == 'idle' else None
                return
        elif not config['chip'] or not config['target_config'] or not config['openocd']:
            message = ('该工程配置了保护区，使用 OpenOCD 保留原有扇区保护规则。'
                       if config['backend'] == 'jlink' else '')
            self.status.setText(message + '请在工程设置中填写芯片型号、OpenOCD 程序和芯片配置文件。')
            self.append_status(self.status.text())
            self.edit_settings() if self.session_state == 'idle' else None
            return
        if config['backend'] == 'openocd':
            W.QMessageBox.warning(self, '请选择探针', '烧录会启动独立的 OpenOCD。请先关闭已有 OpenOCD 服务，选择 DAPLink 或 J-Link。')
            return
        if config['backend'] == 'daplink' and not isinstance(self.cmbDLL.currentData(), int):
            self.status.setText('请先连接并选择 DAPLink，再打开烧录。')
            return
        dialog = FlashDialog(config, self)
        if dialog.exec() != W.QDialog.Accepted:
            return
        self.project.update({k: dialog.config[k] for k in ('chip', 'image', 'bin_address', 'reset_after_flash')})
        self.chip_label.setText('芯片：' + self.project['chip'])
        self.reconnect_after_flash = dialog.reset.isChecked() and dialog.resume.isChecked()
        config = dict(dialog.config)
        config['speed_khz'] = int(config['speed'].split()[0]) * 1000
        self.flash_pending = (config, dialog.image)
        if self.worker is not None:
            self.stop_session()
        else:
            self.launch_flash()

    def launch_flash(self):
        config, image = self.flash_pending
        self.flash_pending = None
        self.flash_job = FlashJob(config, image, self)
        self.flash_job.progress.connect(self.flash_stage)
        self.flash_job.finished.connect(self.flash_finished)
        self.flash_progress.setValue(0)
        self.flash_progress.show()
        self.cancel_flash.show()
        self.cancel_flash.setEnabled(True)
        self.update_capture_controls()
        self.flash_job.start()

    def flash_stage(self, value, message):
        self.flash_progress.setValue(value)
        self.status.setText(message)
        self.append_status('[烧录] ' + message)

    def cancel_programming(self):
        if self.flash_job:
            self.flash_job.cancel.set()
            self.cancel_flash.setEnabled(False)
            self.status.setText('正在取消烧录…')

    def flash_finished(self):
        job = self.flash_job
        self.flash_job = None
        self.cancel_flash.hide()
        if job.error:
            self.reconnect_after_flash = False
            self.status.setText('烧录失败：' + job.error.splitlines()[0][:120] + '；详情见“上位机日志”。')
            self.append_status('[烧录失败] ' + job.error)
        else:
            self.status.setText('烧录与校验完成。' + ('设备已复位运行。' if job.config['reset_after_flash'] else '设备保持暂停；可在下次烧录时选择复位运行。'))
        job.deleteLater()
        self.update_capture_controls()
        if self.closing:
            self.close()
        elif self.reconnect_after_flash:
            if self.linElf.text().strip():
                self.start_elf_load(self.linElf.text().strip())
            else:
                self.reconnect_after_flash = False
                self.on_btnOpen_clicked()

    def finish_session(self):
        super().finish_session()
        if self.session_state == 'retrying':
            self.status.setText('通信异常，正在自动恢复；可点击“取消自动恢复”停止。')
            self.refresh_rtt_status()
            return
        if self.flash_pending and not self.closing:
            self.launch_flash()
        elif self.ready:
            self.status.setText('连接已关闭，可调整监控项或切换工程。错误详情见“上位机日志”栏目。')

    def closeEvent(self, event):
        self.closing = True
        self.stop_probe_discovery()
        if self.worker is not None or self.loader is not None or self.flash_job is not None:
            self.closing = True
            self.flash_pending = None
            if self.worker:
                self.stop_session()
            if self.flash_job:
                self.cancel_programming()
            event.ignore()
            return
        self.tmrRTT.stop()
        self.close_recordings()
        project = self.capture_project()
        try:
            if self.project_path:
                save_project(self.project_path, project)
            layout = self.dock_workspace.snapshot()
            Path('workbench-state.json').write_text(json.dumps({'project': self.project_path, 'draft': project, 'layout': layout}, ensure_ascii=False), encoding='utf-8')
        except OSError as exc:
            self.append_status('工程配置保存失败：' + str(exc))
        event.accept()
