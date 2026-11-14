"""Reusable shell widgets: cards, pills, step strips, checklist, log dock, Z envelope."""

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QTabWidget, QVBoxLayout, QWidget, QSizePolicy,
)

from . import theme


def label(text="", *, role=None, mono=False, wrap=False):
    w = QLabel(text)
    if role:
        w.setObjectName(role)
    if mono:
        w.setFont(QFont("Consolas", 10))
    if wrap:
        w.setWordWrap(True)
    return w


def hline(*widgets, stretch=None, spacing=8):
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for index, widget in enumerate(widgets):
        layout.addWidget(widget, stretch[index] if stretch else 0)
    return row


def vbox(widget, spacing=6, margins=0):
    layout = QVBoxLayout(widget)
    layout.setSpacing(spacing)
    if isinstance(margins, int):
        layout.setContentsMargins(margins, margins, margins, margins)
    else:
        layout.setContentsMargins(*margins)
    return layout


def hbox(widget, spacing=8, margins=0):
    layout = QHBoxLayout(widget)
    layout.setSpacing(spacing)
    if isinstance(margins, int):
        layout.setContentsMargins(margins, margins, margins, margins)
    else:
        layout.setContentsMargins(*margins)
    return layout


class Card(QFrame):
    """White rounded panel; optional bold heading on the first row."""

    def __init__(self, title=None, trailing=None):
        super().__init__()
        self.setObjectName("card")
        self._lay = vbox(self, spacing=8, margins=(14, 12, 14, 12))
        if title is not None:
            head = QWidget()
            head.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            head_lay = hbox(head, margins=0)
            head_lay.addWidget(label(title, role="h3"))
            head_lay.addStretch(1)
            if trailing is not None:
                head_lay.addWidget(trailing)
            self._lay.addWidget(head)

    def add(self, widget):
        self._lay.addWidget(widget)
        return widget

    def add_layout(self, layout):
        self._lay.addLayout(layout)
        return layout


class Pill(QLabel):
    def __init__(self, text="", kind="mute"):
        super().__init__(text)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.set_kind(kind, text)

    def set_kind(self, kind, text=None):
        bg, fg, border = theme.PILL_STYLES.get(kind, theme.PILL_STYLES["mute"])
        self.setStyleSheet(
            f"background:{bg}; color:{fg}; border:1px solid {border}; border-radius:9px;"
            " padding:1px 8px; font-size:11px; font-weight:600;")
        if text is not None:
            self.setText(text)


class Chip(QLabel):
    """Top-bar status chip; kind None = neutral."""

    def __init__(self, text=""):
        super().__init__(text)
        self.set_kind(None)

    def set_kind(self, kind):
        colors = {"real": (theme.ACCENT_SOFT, theme.ACCENT_INK, "#c5d9f5"),
                  "warn": (theme.WARN_SOFT, theme.WARN, theme.WARN_LINE),
                  "bad": (theme.BAD_SOFT, theme.BAD, theme.BAD_LINE),
                  "ok": (theme.OK_SOFT, theme.OK, "#bfe3d0")}
        bg, fg, border = colors.get(kind, ("#f7f9fb", theme.INK, theme.LINE))
        weight = "600" if kind else "400"
        self.setStyleSheet(
            f"background:{bg}; color:{fg}; border:1px solid {border}; border-radius:11px;"
            f" padding:3px 10px; font-size:12px; font-weight:{weight};")


class StepStrip(QWidget):
    """Compact numbered stepper: 1 设备就绪  2 示教 Z  3 检查 …"""

    def __init__(self, steps):
        super().__init__()
        self._lay = hbox(self, spacing=0, margins=0)
        self._labels = []
        for index, step in enumerate(steps):
            w = QLabel(step)
            if index == 0:
                radius = "8px 0 0 8px"
            elif index == len(steps) - 1:
                radius = "0 8px 8px 0"
            else:
                radius = "0"
            w.setProperty("radius", radius)
            self._lay.addWidget(w)
            self._labels.append(w)
        self.set_states(["todo"] * len(steps))

    def set_states(self, states):
        for w, state, last in zip(self._labels, states,
                                  [False] * (len(states) - 1) + [True]):
            bg, fg, border = "#ffffff", theme.MUTED, theme.LINE
            if state == "done":
                bg, fg = theme.OK_SOFT, theme.OK
            elif state == "current":
                bg, fg, border = theme.ACCENT, "#ffffff", theme.ACCENT
            elif state in ("warn", "bad"):
                bg, fg, border = theme.BAD_SOFT, theme.BAD, theme.BAD_LINE
            right = f"border-right:1px solid {border};" if last else ""
            w.setStyleSheet(
                f"background:{bg}; color:{fg}; border:1px solid {border}; border-right:none; {right}"
                f" border-radius:{w.property('radius')}; padding:5px 12px; font-size:12px;"
                + (" font-weight:600;" if state in ("current", "warn") else ""))


class CheckItem(QWidget):
    """One readiness row: state icon, title, detail, optional manual checkbox."""

    def __init__(self, title, detail="", *, manual=False, manual_text="我已确认"):
        super().__init__()
        lay = hbox(self, spacing=9, margins=0)
        lay.setAlignment(Qt.AlignTop)
        self.icon = QLabel()
        self.icon.setFixedSize(16, 16)
        self.icon.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.icon, 0, Qt.AlignTop)
        body = QWidget()
        body_lay = vbox(body, spacing=2, margins=0)
        self.title = label(title, wrap=True)
        self.detail = label(detail, role="muted", wrap=True)
        self.detail.setStyleSheet("font-size:11px")
        body_lay.addWidget(self.title)
        body_lay.addWidget(self.detail)
        self.check = None
        if manual:
            self.check = QCheckBox(manual_text)
            self.check.setStyleSheet(
                f"background:{theme.CARD}; border:1px solid {theme.LINE}; border-radius:6px; padding:4px 8px;")
            body_lay.addWidget(self.check)
        lay.addWidget(body, 1)
        self.set_state("todo")

    def set_state(self, state, detail=None):
        bg, mark, fg = theme.CHECK_ICONS[state]
        border = fg if state == "todo" else bg
        self.icon.setText(mark)
        self.icon.setStyleSheet(
            f"background:{bg}; color:{fg}; border:1.5px solid {border}; border-radius:8px;"
            " font-weight:700; font-size:10px;")
        if detail is not None:
            self.detail.setText(detail)


class Checklist(QWidget):
    """Vertical list of CheckItems with a 'done / total' counter."""

    def __init__(self):
        super().__init__()
        self.lay = vbox(self, spacing=0, margins=0)
        self.items = []

    def set_items(self, items):
        while self.lay.count():
            self.lay.takeAt(0).widget().deleteLater()
        self.items = items
        for index, item in enumerate(items):
            if index:
                sep = QFrame()
                sep.setFrameShape(QFrame.HLine)
                sep.setStyleSheet("color:#e6eaef; border:none; border-top:1px dashed #e6eaef; max-height:1px;")
                self.lay.addWidget(sep)
            wrap = QWidget()
            wrap_lay = vbox(wrap, spacing=0, margins=(0, 5, 0, 5))
            wrap_lay.addWidget(item)
            self.lay.addWidget(wrap)

    def counts(self):
        done = sum(1 for i in self.items if i.icon.text() == "✓")
        return done, len(self.items)

    def manual_ok(self):
        return all(i.check is None or i.check.isChecked() for i in self.items)


class ZEnvelope(QWidget):
    """Vertical Z ruler: taught AF bands, current position, stray stack limits."""

    def __init__(self):
        super().__init__()
        self.setMinimumWidth(200)
        self._bands = []          # (label, z_um, half_um, color)
        self._current = None      # µm
        self._stray = []          # (label, z_um) off-envelope markers

    def set_data(self, bands, current=None, stray=()):
        self._bands = list(bands)
        self._current = current
        self._stray = list(stray)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        axis_x, top, bottom = 62, 30, h - 14
        values = [z for _, z, half, _ in self._bands for z in (z - half, z + half)]
        values += [self._current] if self._current is not None else []
        if len(values) < 2:
            painter.setPen(QColor(theme.MUTED))
            painter.drawText(self.rect(), Qt.AlignCenter, "示教后显示 Z 包络")
            painter.end()
            return
        lo, hi = min(values), max(values)
        span = max(hi - lo, 1.0)
        lo, hi = lo - span * .18, hi + span * .18
        span = hi - lo

        def y_of(z):
            return bottom - (z - lo) / span * (bottom - top)

        mono = QFont("Consolas", 8)
        painter.setPen(QPen(QColor("#c3ccd6"), 2))
        painter.drawLine(axis_x, top - 14, axis_x, bottom)
        painter.setFont(mono)
        for frac in (0, .33, .66, 1):
            z = lo + frac * span
            y = y_of(z)
            painter.setPen(QPen(QColor("#c3ccd6"), 1))
            painter.drawLine(axis_x - 4, int(y), axis_x + 4, int(y))
            painter.setPen(QColor(theme.MUTED))
            painter.drawText(0, int(y) + 3, axis_x - 8, 12, Qt.AlignRight | Qt.AlignVCenter, f"{z:.0f}")
        for text, z, half, color in self._bands:
            color = QColor(color)
            y1, y2 = y_of(z + half), y_of(z - half)
            fill = QColor(color)
            fill.setAlpha(46)
            painter.fillRect(axis_x + 4, int(y1), 42, max(4, int(y2 - y1)), fill)
            painter.setPen(QPen(color, 2))
            painter.drawLine(axis_x + 4, int(y_of(z)), axis_x + 46, int(y_of(z)))
            painter.drawText(axis_x + 52, int(y_of(z)) - 6, w - axis_x - 56, 12,
                             Qt.AlignLeft, text)
            painter.setPen(color)
            painter.drawText(axis_x + 52, int(y_of(z)) + 5, w - axis_x - 56, 12,
                             Qt.AlignLeft, f"{z:.2f}")
        if self._current is not None:
            y = y_of(self._current)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.INK))
            from PySide6.QtGui import QPolygonF
            from PySide6.QtCore import QPointF
            painter.drawPolygon(QPolygonF([QPointF(axis_x - 14, y - 5), QPointF(axis_x - 14, y + 5), QPointF(axis_x - 4, y)]))
            painter.setPen(QColor(theme.INK))
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            painter.drawText(0, int(y) - 18, axis_x - 6, 12, Qt.AlignRight, "当前")
        for text, z in self._stray:
            painter.setPen(QColor(theme.WARN))
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            painter.drawText(axis_x + 8, top - 16, w - axis_x - 12, 12,
                             Qt.AlignLeft, f"↑ {text} {z:.1f}（包络外）")
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.WARN))
            from PySide6.QtGui import QPolygonF
            from PySide6.QtCore import QPointF
            painter.drawPolygon(QPolygonF([QPointF(axis_x - 5, top - 14), QPointF(axis_x + 5, top - 14), QPointF(axis_x, top - 22)]))
        painter.end()


class LogDock(QTabWidget):
    """Bottom dock: event log / rpc trace / diagnostics."""

    LEVELS = {"info": ("信息", theme.MUTED), "ok": ("完成", theme.OK),
              "warn": ("注意", theme.WARN), "error": ("错误", theme.BAD),
              "move": ("运动", "#7a4700"), "lock": ("锁定", theme.BAD)}

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setFixedHeight(144)
        self.collapsed = False
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.trace = QPlainTextEdit()
        self.trace.setReadOnly(True)
        self.trace.setPlaceholderText("切换到此页签时刷新当前设备的 RPC 调用记录")
        self.diag = QPlainTextEdit()
        self.diag.setReadOnly(True)
        self.diag.setPlaceholderText("最近一次只读检查 / 采图诊断报告")
        self.addTab(self.log, "事件日志")
        self.addTab(self.trace, "RPC 追踪")
        self.addTab(self.diag, "诊断")
        corner = QWidget()
        corner_lay = hbox(corner, spacing=6, margins=(0, 0, 8, 0))
        corner_lay.addStretch(1)
        self.fold_button = QPushButton('收起日志', clicked=self.toggle_fold)
        corner_lay.addWidget(self.fold_button)
        for text, fn in (("刷新", self.refresh_tab), ("复制", self.copy_current),
                         ("导出诊断包…", app.export_diagnostic_bundle)):
            corner_lay.addWidget(QPushButton(text, clicked=fn))
        self.setCornerWidget(corner)
        self.currentChanged.connect(lambda *_: self.refresh_tab())

    def toggle_fold(self):
        self.collapsed = not self.collapsed
        self.setFixedHeight(36 if self.collapsed else 144)
        self.fold_button.setText('展开日志' if self.collapsed else '收起日志')

    def add(self, level, text):
        from datetime import datetime
        name, color = self.LEVELS.get(level, self.LEVELS["info"])
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"{stamp}  {name:<4} {text}")

    def refresh_tab(self):
        index = self.currentIndex()
        if index == 1:
            self.trace.setPlainText(self.app.rpc_trace_text())
        elif index == 2:
            self.diag.setPlainText(self.app.diagnostic_text())

    def copy_current(self):
        widget = (self.log, self.trace, self.diag)[self.currentIndex()]
        widget.selectAll()
        widget.copy()
        cursor = widget.textCursor()
        cursor.clearSelection()
        widget.setTextCursor(cursor)


def note(text, *, kind="warn"):
    bg, border, fg = (theme.WARN_SOFT, theme.WARN_LINE, "#7a4700") if kind == "warn" \
        else (theme.ACCENT_SOFT, "#c5d9f5", theme.ACCENT_INK)
    w = label(text, wrap=True)
    w.setStyleSheet(
        f"background:{bg}; color:{fg}; border:1px solid {border}; border-radius:7px;"
        " padding:6px 9px; font-size:11.5px;")
    return w
