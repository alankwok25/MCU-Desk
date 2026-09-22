"""Live waveform inspection, following the user's data_plot2 mouse bindings."""
from bisect import bisect_left
import csv
import math

from PyQt5 import QtCore as C, QtGui as G, QtWidgets as W
from PyQt5.QtCore import Qt
from PyQt5.QtChart import QChart, QChartView, QLineSeries, QValueAxis


HELP = ('滚轮：平移 X　Ctrl + 滚轮：缩放 X　Shift + 滚轮：缩放 Y\n'
        'Ctrl + Shift + 滚轮：平移 Y　右键拖动：自由平移\n'
        '左键拖动：选择 X 范围　右键双击：适配全部　Esc：清除选区\n'
        '移动鼠标：吸附采样点并显示原始值；点击图例可隐藏曲线。\n'
        '手动操作暂停画面跟随，后台采集与 RTT 继续。点击“实时跟随”回到最新数据。\n'
        '选区统计和导出使用当前画面缓存（每条最多 1000 点）的原始数据。')


def padded_range(values):
    values = [v for v in values if math.isfinite(v)]
    if not values:
        return (0., 1.)
    lo, hi = min(values), max(values)
    padding = (hi - lo) * .05 if hi > lo else max(abs(lo) * .01, .5)
    return lo - padding, hi + padding


def zoom_range(bounds, anchor, factor, full):
    lo, hi = bounds
    span = min(max((hi - lo) * factor, (full[1] - full[0]) / 1e6), full[1] - full[0])
    fraction = (anchor - lo) / (hi - lo)
    start = min(max(anchor - span * fraction, full[0]), full[1] - span)
    return start, start + span


class WaveformView(QChartView):
    follow_changed = C.pyqtSignal(bool)
    selection_changed = C.pyqtSignal()
    COLORS = ('#1877d2', '#d85a21', '#258544', '#9c43ac', '#ba8b12', '#0098a3', '#cd426f', '#626dc2')

    def __init__(self, parent=None):
        super().__init__(QChart(), parent)
        self.setRenderHint(G.QPainter.Antialiasing)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFrameShape(W.QFrame.NoFrame)
        chart = self.chart()
        chart.setAnimationOptions(QChart.NoAnimation)
        chart.setMargins(C.QMargins(2, 2, 2, 2))
        chart.layout().setContentsMargins(0, 0, 0, 0)
        chart.setBackgroundRoundness(0)
        chart.legend().setFont(G.QFont('Microsoft YaHei UI', 8))
        chart.legend().setAlignment(Qt.AlignTop)
        self.x_axis, self.y_axis = QValueAxis(), QValueAxis()
        chart.addAxis(self.x_axis, Qt.AlignBottom)
        chart.addAxis(self.y_axis, Qt.AlignLeft)
        for axis in (self.x_axis, self.y_axis):
            axis.setLabelsFont(G.QFont('Microsoft YaHei UI', 8))
            axis.setTitleFont(G.QFont('Microsoft YaHei UI', 8))
            axis.setLabelFormat('%.5g')
            axis.setTickCount(6)
            axis.setTickType(QValueAxis.TicksDynamic)
            axis.setTickAnchor(0)
            axis.setTickInterval(.2)
            axis.setMinorTickCount(1)
            axis.setGridLinePen(G.QPen(G.QColor('#dbe2e9'), 1))
            axis.setMinorGridLinePen(G.QPen(G.QColor('#eef1f5'), 1))
        self.raw, self.names, self.lines, self.lookup = {}, {}, {}, {}
        self.hidden, self.transforms = set(), {}
        self.times, self.hover_x = [], None
        self.selection, self.drag = None, None
        self.full_x, self.full_y = (0., 1.), (0., 1.)
        self.x_title = '采样序号'
        self.follow = True
        self.show_labels = True
        self.follow_action = W.QAction('实时跟随', self)
        self.follow_action.setCheckable(True)
        self.follow_action.setChecked(True)
        self.follow_action.toggled.connect(self.set_follow)
        self.fit_action = W.QAction('适配全部', self)
        self.fit_action.triggered.connect(self.fit_all)
        self.fit_selection_action = W.QAction('适配选区', self)
        self.fit_selection_action.triggered.connect(self.fit_selection)
        self.clear_action = W.QAction('清除选区', self)
        self.clear_action.triggered.connect(self.clear_selection)
        self.stats_action = W.QAction('选区统计…', self)
        self.stats_action.triggered.connect(self.show_statistics)
        self.export_action = W.QAction('导出选区原始数据…', self)
        self.export_action.triggered.connect(self.choose_export)
        self.labels_action = W.QAction('显示游标数值标签', self)
        self.labels_action.setCheckable(True)
        self.labels_action.setChecked(True)
        self.labels_action.toggled.connect(self.set_labels)
        self.curves_menu = W.QMenu('曲线显示', self)
        self.curves_menu.aboutToShow.connect(self.build_curves_menu)
        self.help_action = W.QAction('波形操作说明', self)
        self.help_action.triggered.connect(lambda: W.QMessageBox.information(self, '波形操作', HELP))
        self.clear_selection()

    def add_actions(self, menu):
        for action in (self.follow_action, self.fit_action, self.fit_selection_action, self.clear_action,
                       self.stats_action, self.export_action, self.labels_action):
            menu.addAction(action)
        menu.addMenu(self.curves_menu)
        menu.addSeparator()
        menu.addAction(self.help_action)

    def toolbar(self):
        bar = W.QWidget()
        layout = W.QHBoxLayout(bar)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(3)
        for action in (self.follow_action, self.fit_action, self.fit_selection_action):
            button = W.QToolButton()
            button.setDefaultAction(action)
            layout.addWidget(button)
        more = W.QToolButton()
        more.setText('更多')
        menu = W.QMenu(more)
        for action in (self.clear_action, self.stats_action, self.export_action, self.labels_action):
            menu.addAction(action)
        menu.addMenu(self.curves_menu)
        menu.addAction(self.help_action)
        more.setMenu(menu)
        more.setPopupMode(W.QToolButton.InstantPopup)
        layout.addWidget(more)
        self.mode_label = W.QLabel('实时 · 最近 1000 点')
        self.mode_label.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Preferred)
        layout.addWidget(self.mode_label, 1)
        bar.setToolTip(HELP)
        return bar

    def set_follow(self, enabled):
        changed = self.follow != enabled
        self.follow = enabled
        self.follow_action.blockSignals(True)
        self.follow_action.setChecked(enabled)
        self.follow_action.blockSignals(False)
        if hasattr(self, 'mode_label'):
            self.mode_label.setText('实时 · 最近 1000 点' if enabled else '画面暂停 · 后台采集继续')
        if enabled:
            self.hover_x = None
            self.clear_selection()
        if changed:
            self.follow_changed.emit(enabled)

    def reset_data(self):
        self.raw, self.lookup, self.names = {}, {}, {}
        self.hidden.clear()
        self.transforms.clear()
        self.hover_x, self.times = None, []
        self.clear_selection()
        self.rebuild_lines()
        self.fit_all()
        self.set_follow(True)

    def set_live_data(self, data, names, x_title):
        if not self.follow:
            return
        self.raw = {row: tuple(points) for row, points in data.items()}
        self.names = dict(names)
        self.x_title = x_title
        self.lookup = {row: dict(points) for row, points in self.raw.items()}
        self.times = sorted({x for points in self.raw.values() for x, _ in points})
        self.rebuild_lines()
        self.fit_all()

    def visible_rows(self):
        return [row for row in self.raw if row not in self.hidden]

    def transformed(self, row, value):
        scale, offset = self.transforms.get(row, (1., 0.))
        return value * scale + offset

    def rebuild_lines(self):
        chart = self.chart()
        # Dense multi-curve views benefit more from responsive input than antialiasing.
        self.setRenderHint(G.QPainter.Antialiasing, len(self.visible_rows()) <= 12)
        for row in list(self.lines):
            if row not in self.raw:
                for series in self.lines.pop(row):
                    chart.removeSeries(series)
                    series.deleteLater()
        for row, samples in self.raw.items():
            runs = [[]]
            for x, value in samples:
                y = self.transformed(row, value)
                if math.isfinite(y):
                    runs[-1].append(C.QPointF(x, y))
                elif runs[-1]:
                    runs.append([])
            runs = [run for run in runs if run] or [[]]
            existing = self.lines.setdefault(row, [])
            while len(existing) < len(runs):
                series = QLineSeries()
                series.setName(self.names.get(row, str(row)))
                chart.addSeries(series)
                series.attachAxis(self.x_axis)
                series.attachAxis(self.y_axis)
                series.setPen(G.QPen(G.QColor(self.COLORS[row % len(self.COLORS)]), 1.4))
                marker = chart.legend().markers(series)[0]
                marker.clicked.connect(lambda r=row: self.toggle_curve(r))
                existing.append(series)
            while len(existing) > len(runs):
                series = existing.pop()
                chart.removeSeries(series)
                series.deleteLater()
            for index, (series, points) in enumerate(zip(existing, runs)):
                series.replace(points)
                series.setPointsVisible(len(points) == 1)
                series.setVisible(row not in self.hidden)
                marker = chart.legend().markers(series)[0]
                marker.setVisible(index == 0)
                marker.setLabelBrush(G.QBrush(G.QColor('#9ca3af' if row in self.hidden else '#27364b')))
        chart.legend().setVisible(len(self.raw) <= 8)
        self.x_axis.setTitleText(self.x_title)
        self.viewport().update()

    def fit_all(self):
        self.full_x = padded_range(self.times)
        self.full_y = padded_range(self.transformed(row, y) for row in self.visible_rows()
                                   for _, y in self.raw[row])
        self.set_ranges(self.full_x, self.full_y)

    def ranges(self):
        return (self.x_axis.min(), self.x_axis.max()), (self.y_axis.min(), self.y_axis.max())

    def set_ranges(self, x, y):
        if all(math.isfinite(v) for v in (*x, *y)) and x[1] > x[0] and y[1] > y[0]:
            for axis, bounds in ((self.x_axis, x), (self.y_axis, y)):
                raw_step = (bounds[1] - bounds[0]) / 6
                base = 10 ** math.floor(math.log10(raw_step))
                step = next((n * base for n in (1, 2, 2.5, 5, 10) if n * base >= raw_step), 10 * base)
                axis.setTickInterval(step)
            self.x_axis.setRange(*x)
            self.y_axis.setRange(*y)
            self.viewport().update()

    def plot_rect(self):
        rect = self.chart().plotArea()
        top = self.mapFromScene(self.chart().mapToScene(rect.topLeft()))
        bottom = self.mapFromScene(self.chart().mapToScene(rect.bottomRight()))
        return C.QRectF(top, bottom)

    def value_at(self, point):
        rect = self.plot_rect()
        (xl, xh), (yl, yh) = self.ranges()
        return C.QPointF(xl + (point.x() - rect.left()) / max(rect.width(), 1) * (xh - xl),
                         yh - (point.y() - rect.top()) / max(rect.height(), 1) * (yh - yl))

    def pixel_at(self, x, y):
        rect = self.plot_rect()
        (xl, xh), (yl, yh) = self.ranges()
        return C.QPointF(rect.left() + (x - xl) / (xh - xl) * rect.width(),
                         rect.bottom() - (y - yl) / (yh - yl) * rect.height())

    def navigate_wheel(self, steps, modifiers, anchor):
        if not self.times or not steps:
            return
        x, y = self.ranges()
        ctrl, shift = bool(modifiers & Qt.ControlModifier), bool(modifiers & Qt.ShiftModifier)
        factor = .8 ** max(-20, min(20, steps))
        axis = 1 if shift else 0
        bounds, full = (y, self.full_y) if axis else (x, self.full_x)
        if ctrl != shift:
            bounds = zoom_range(bounds, anchor.y() if axis else anchor.x(), factor, full)
        else:
            span = bounds[1] - bounds[0]
            offset = -steps * span * .15
            start = min(max(bounds[0] + offset, full[0]), full[1] - span)
            bounds = start, start + span
        if bounds != (y if axis else x):
            self.set_follow(False)
        self.set_ranges(x if axis else bounds, bounds if axis else y)

    def wheelEvent(self, event):
        if self.plot_rect().contains(event.pos()):
            self.navigate_wheel(event.angleDelta().y() / 120., event.modifiers(), self.value_at(event.pos()))
            event.accept()
        else:
            event.ignore()

    def mousePressEvent(self, event):
        if self.times and self.plot_rect().contains(event.pos()) and event.button() in (Qt.LeftButton, Qt.RightButton):
            self.setFocus()
            self.set_follow(False)
            self.drag = (event.button(), C.QPoint(event.pos()), self.ranges(), self.value_at(event.pos()).x())
            self.hover_x = None
            if event.button() == Qt.LeftButton:
                self.selection = None
            else:
                self.setCursor(Qt.SizeAllCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.drag:
            button, start, (x, y), x_start = self.drag
            if button == Qt.LeftButton:
                now = min(max(self.value_at(event.pos()).x(), self.full_x[0]), self.full_x[1])
                self.selection = tuple(sorted((x_start, now)))
            else:
                rect = self.plot_rect()
                dx = -(event.x() - start.x()) / max(rect.width(), 1) * (x[1] - x[0])
                dy = (event.y() - start.y()) / max(rect.height(), 1) * (y[1] - y[0])
                new_x, new_y = (x[0] + dx, x[1] + dx), (y[0] + dy, y[1] + dy)
                self.full_x = min(self.full_x[0], new_x[0]), max(self.full_x[1], new_x[1])
                self.full_y = min(self.full_y[0], new_y[0]), max(self.full_y[1], new_y[1])
                self.set_ranges(new_x, new_y)
            self.viewport().update()
            event.accept()
        elif self.times and self.plot_rect().contains(event.pos()):
            self.hover_x = self.nearest_x(self.value_at(event.pos()).x())
            self.viewport().setToolTip(self.hover_text())
            self.viewport().update()
        else:
            self.hover_x = None
            self.viewport().update()
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.drag and event.button() == self.drag[0]:
            if self.drag[0] == Qt.LeftButton and abs(event.x() - self.drag[1].x()) < 4:
                self.selection = None
            self.drag = None
            self.unsetCursor()
            self.update_selection_actions()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.RightButton:
            self.drag = None
            self.unsetCursor()
            self.fit_all()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event):
        if not self.drag:
            self.hover_x = None
            self.viewport().update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.drag = None
            self.unsetCursor()
            self.clear_selection()
            event.accept()
        else:
            super().keyPressEvent(event)

    def nearest_x(self, value):
        if not self.times:
            return None
        index = bisect_left(self.times, value)
        candidates = self.times[max(0, index - 1):index + 1]
        return min(candidates, key=lambda x: abs(x - value))

    def hover_values(self):
        return [(row, self.lookup[row].get(self.hover_x, math.nan)) for row in self.visible_rows()]

    def hover_text(self):
        lines = ['%s = %.9g' % (self.x_title, self.hover_x)] if self.hover_x is not None else []
        for row, value in self.hover_values():
            text = self.names[row] + ' = ' + ('%.10g' % value if math.isfinite(value) else '—')
            if row in self.transforms:
                text += '（显示 %.10g）' % self.transformed(row, value)
            lines.append(text)
        return '\n'.join(lines)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = G.QPainter(self.viewport())
        rect = self.plot_rect()
        painter.setClipRect(rect)
        if self.selection:
            a, b = (self.pixel_at(x, 0).x() for x in self.selection)
            painter.fillRect(C.QRectF(a, rect.top(), b - a, rect.height()), G.QColor(40, 110, 220, 35))
        if self.hover_x is not None:
            x = self.pixel_at(self.hover_x, 0).x()
            painter.setPen(G.QPen(G.QColor('#65758a'), 1, Qt.DashLine))
            painter.drawLine(C.QPointF(x, rect.top()), C.QPointF(x, rect.bottom()))
            painter.setFont(G.QFont('Microsoft YaHei UI', 8))
            timestamp = '%s = %.8g' % (self.x_title, self.hover_x)
            stamp_rect = C.QRectF(rect.left() + 4, rect.top() + 3,
                                  min(rect.width() - 8, painter.fontMetrics().horizontalAdvance(timestamp) + 8), 19)
            painter.fillRect(stamp_rect, G.QColor(255, 255, 255, 230))
            painter.drawText(stamp_rect, Qt.AlignCenter, timestamp)
            occupied = [stamp_rect]
            for row, value in self.hover_values():
                if not math.isfinite(value):
                    continue
                point = self.pixel_at(self.hover_x, self.transformed(row, value))
                if not rect.contains(point):
                    continue
                color = G.QColor(self.COLORS[row % len(self.COLORS)])
                painter.setPen(G.QPen(color, 1))
                painter.setBrush(color)
                painter.drawEllipse(point, 3., 3.)
                if not self.show_labels or len(occupied) >= 12:
                    continue
                text = '%s = %.8g' % (self.names[row], value)
                width = min(painter.fontMetrics().horizontalAdvance(text) + 8, rect.width() - 4)
                label = C.QRectF(min(point.x() + 8, rect.right() - width), max(rect.top(), point.y() - 22), width, 19)
                while any(label.intersects(other) for other in occupied) and label.bottom() < rect.bottom() - 20:
                    label.translate(0, 20)
                if any(label.intersects(other) for other in occupied) or label.bottom() > rect.bottom():
                    continue
                painter.fillRect(label, G.QColor(255, 255, 255, 225))
                painter.drawText(label.adjusted(4, 0, -2, 0), Qt.AlignVCenter,
                                 painter.fontMetrics().elidedText(text, Qt.ElideRight, int(width - 6)))
                occupied.append(label)
        painter.end()

    def update_selection_actions(self):
        enabled = self.selection is not None and self.selection[1] > self.selection[0]
        for action in (self.fit_selection_action, self.clear_action, self.stats_action, self.export_action):
            action.setEnabled(enabled)
        self.selection_changed.emit()
        self.viewport().update()

    def clear_selection(self):
        self.selection = None
        self.update_selection_actions()

    def fit_selection(self):
        if self.selection and self.selection[1] > self.selection[0]:
            self.set_follow(False)
            self.set_ranges(self.selection, self.ranges()[1])

    def statistics(self):
        if not self.selection:
            return []
        lo, hi = self.selection
        result = []
        for row in self.visible_rows():
            values = [v for x, v in self.raw[row] if lo <= x <= hi and math.isfinite(v)]
            result.append((self.names[row], len(values), min(values) if values else None,
                           max(values) if values else None, sum(values) / len(values) if values else None))
        return result

    def show_statistics(self):
        if not self.selection:
            return
        dialog = W.QDialog(self)
        dialog.setWindowTitle('选区统计 · 原始数据')
        dialog.resize(680, 360)
        layout = W.QVBoxLayout(dialog)
        layout.addWidget(W.QLabel('%s：%.9g — %.9g' % (self.x_title, *self.selection)))
        table = W.QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(['曲线', '有效点数', '最小值', '最大值', '平均值'])
        table.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
        for values in self.statistics():
            r = table.rowCount()
            table.insertRow(r)
            for c, value in enumerate(values):
                table.setItem(r, c, W.QTableWidgetItem('—' if value is None else str(value)))
        table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeToContents)
        layout.addWidget(table)
        close = W.QPushButton('关闭')
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def export_selection(self, path):
        if not self.selection:
            raise ValueError('请先用左键拖动选择范围')
        lo, hi = self.selection
        rows = self.visible_rows()
        with open(path, 'w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.writer(stream)
            writer.writerow([self.x_title] + [self.names[row] for row in rows])
            for x in self.times:
                if lo <= x <= hi:
                    values = [self.lookup[row].get(x, math.nan) for row in rows]
                    writer.writerow([x] + [v if math.isfinite(v) else '' for v in values])

    def choose_export(self):
        path, _ = W.QFileDialog.getSaveFileName(self, '导出选区原始数据', 'wave-selection.csv', 'CSV (*.csv)')
        if path:
            try:
                self.export_selection(path)
            except (OSError, ValueError) as exc:
                W.QMessageBox.warning(self, '导出失败', str(exc))

    def set_labels(self, enabled):
        self.show_labels = enabled
        self.viewport().update()

    def toggle_curve(self, row):
        if row in self.hidden:
            self.hidden.remove(row)
        else:
            self.hidden.add(row)
        self.rebuild_lines()
        if self.follow:
            self.fit_all()

    def build_curves_menu(self):
        self.curves_menu.clear()
        for text, mode in (('全部显示', 'show'), ('全部隐藏', 'hide'), ('反选', 'invert')):
            self.curves_menu.addAction(text, lambda checked=False, m=mode: self.show_curves(m))
        self.curves_menu.addSeparator()
        for row in self.raw:
            action = self.curves_menu.addAction(self.names[row])
            action.setCheckable(True)
            action.setChecked(row not in self.hidden)
            action.triggered.connect(lambda checked, r=row: self.toggle_curve(r))
        transforms = self.curves_menu.addMenu('显示缩放 / 偏移…')
        for row in self.raw:
            transforms.addAction(self.names[row], lambda checked=False, r=row: self.edit_transform(r))

    def show_curves(self, mode):
        self.hidden = set() if mode == 'show' else set(self.raw) if mode == 'hide' else set(self.raw) - self.hidden
        self.rebuild_lines()
        if self.follow:
            self.fit_all()

    def set_transform(self, row, scale, offset):
        if not all(math.isfinite(v) for v in (scale, offset)) or scale == 0:
            raise ValueError('显示比例必须是非零有限数，偏移必须是有限数')
        self.transforms[row] = scale, offset
        self.rebuild_lines()
        self.fit_all()

    def edit_transform(self, row):
        current = self.transforms.get(row, (1., 0.))
        text, ok = W.QInputDialog.getText(self, '显示变换 · 原始数据不变',
                                         self.names[row] + '\n比例, 偏移（例如 1, 0）', text='%g, %g' % current)
        if ok:
            try:
                scale, offset = map(float, text.split(','))
                self.set_transform(row, scale, offset)
            except ValueError as exc:
                W.QMessageBox.warning(self, '显示变换无效', str(exc))
