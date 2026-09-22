"""Native Qt docks; presentation changes never alter acquisition state."""
import base64

from PyQt5 import QtCore, QtWidgets as W
from PyQt5.QtCore import Qt


class DockWorkspace(W.QMainWindow):
    changed = QtCore.pyqtSignal()
    host_shown = QtCore.pyqtSignal()

    def __init__(self, widgets, menu, parent):
        super().__init__(parent, Qt.Widget)
        # QMainWindow's constructor forces the Window flag even with a parent.
        self.setWindowFlags(Qt.Widget)
        self.setDockOptions(self.AllowNestedDocks | self.AllowTabbedDocks | self.AnimatedDocks)
        self.setTabPosition(Qt.AllDockWidgetAreas, W.QTabWidget.North)
        self.docks = {}
        self.actions = {}
        self.updating = False
        for key, title, widget in widgets:
            dock = W.QDockWidget(title, self)
            dock.setObjectName('panel_' + key)
            dock.setAllowedAreas(Qt.AllDockWidgetAreas)
            dock.setWidget(widget)
            dock.setMinimumSize(150, 80)
            dock.setToolTip('拖动标题栏调整位置；双击停靠面板可浮动；从“视图”菜单可收回浮动面板或重新打开。')
            self.docks[key] = dock
            self.addDockWidget(Qt.LeftDockWidgetArea, dock)
            action = dock.toggleViewAction()
            menu.addAction(action)
            self.actions[key] = action
            dock.visibilityChanged.connect(lambda visible, k=key: self.visibility_changed(k, visible))
            dock.dockLocationChanged.connect(self.layout_changed)
            dock.topLevelChanged.connect(self.layout_changed)

    def layout_changed(self, *unused):
        if not self.updating:
            self.changed.emit()

    def visibility_changed(self, key, visible):
        # A dock behind another tab is not visible, but is still enabled in View.
        if visible and key == 'host':
            self.host_shown.emit()
        self.layout_changed()

    def set_visible(self, keys):
        self.updating = True
        try:
            for key, dock in self.docks.items():
                dock.setVisible(key in keys)
            if 'host' in keys:
                self.docks['host'].raise_()
        finally:
            self.updating = False

    def arrange(self, orientation, keys=None):
        """Also provides explicit horizontal/vertical arrangements without dragging."""
        keys = keys if keys is not None else [k for k, d in self.docks.items() if not d.isHidden()]
        if not keys:
            keys = ['logs']
        self.updating = True
        try:
            for key in keys:
                dock = self.docks[key]
                dock.setFloating(False)
                self.removeDockWidget(dock)
            first = self.docks[keys[0]]
            self.addDockWidget(Qt.LeftDockWidgetArea, first)
            previous = first
            for key in keys[1:]:
                dock = self.docks[key]
                self.splitDockWidget(previous, dock, orientation)
                previous = dock
            for key in keys:
                self.docks[key].show()
            self.resizeDocks([self.docks[k] for k in keys], [400] * len(keys), orientation)
        finally:
            self.updating = False
        self.changed.emit()

    def reset_layout(self):
        self.updating = True
        try:
            for dock in self.docks.values():
                dock.setFloating(False)
                self.removeDockWidget(dock)
            watch, wave, logs = (self.docks[k] for k in ('watch', 'wave', 'logs'))
            self.addDockWidget(Qt.LeftDockWidgetArea, watch)
            self.splitDockWidget(watch, wave, Qt.Horizontal)
            self.splitDockWidget(wave, logs, Qt.Vertical)
            self.addDockWidget(Qt.LeftDockWidgetArea, self.docks['browser'])
            self.tabifyDockWidget(watch, self.docks['browser'])
            self.addDockWidget(Qt.RightDockWidgetArea, self.docks['host'])
            self.tabifyDockWidget(logs, self.docks['host'])
            self.set_visible(['watch', 'wave', 'logs'])
            self.resizeDocks([watch, wave], [300, 800], Qt.Horizontal)
            self.resizeDocks([wave, logs], [400, 300], Qt.Vertical)
        finally:
            self.updating = False

    def dock_floating_panels(self):
        for dock in self.docks.values():
            if dock.isFloating():
                shown = not dock.isHidden()
                dock.setFloating(False)
                dock.setVisible(shown)

    def snapshot(self):
        return {'version': 2, 'state': bytes(self.saveState(2).toBase64()).decode('ascii')}

    def restore_snapshot(self, saved):
        if not isinstance(saved, dict) or saved.get('version') != 2:
            return False
        try:
            raw = base64.b64decode(saved['state'], validate=True)
            if not raw or len(raw) > 1024 * 1024:
                return False
        except (KeyError, ValueError, TypeError):
            return False
        self.updating = True
        try:
            if not self.restoreState(QtCore.QByteArray(raw), 2):
                return False
            # A second monitor may have been unplugged since the previous session.
            for dock in self.docks.values():
                if dock.isFloating() and not dock.isHidden():
                    frame = dock.frameGeometry()
                    reachable = any(s.availableGeometry().contains(frame.topLeft()) and
                                    s.availableGeometry().contains(frame.topRight())
                                    for s in W.QApplication.screens())
                    if not reachable:
                        dock.setFloating(False)
            if all(d.isHidden() for d in self.docks.values()):
                self.docks['logs'].show()
            return True
        finally:
            self.updating = False
