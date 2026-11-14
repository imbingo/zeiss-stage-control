"""Frameless title strip using the operating system's move/resize handling."""

from PySide6.QtCore import Qt, QEvent, QObject
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QFrame, QWidget
from .widgets import hbox
from . import theme


class StatusText(QLabel):
    def set_kind(self, kind):
        color = {
            "ok": theme.OK,
            "bad": theme.BAD,
            "warn": theme.WARN,
            "demo": theme.WARN,
        }.get(kind, theme.MUTED)
        self.setStyleSheet(
            f"color:{color}; font-size:11px; padding:0; background:transparent; border:none;"
        )


class WindowChrome(QObject):
    def __init__(self, window, bar):
        super().__init__(window)
        self.window, self.bar = window, bar
        self.margin = 6
        QApplication.instance().installEventFilter(self)

    def edges(self, point):
        w = self.window
        if w.isMaximized() or w.isFullScreen():
            return Qt.Edges()
        edges = Qt.Edges()
        if point.x() < self.margin:
            edges |= Qt.LeftEdge
        if point.x() >= w.width() - self.margin:
            edges |= Qt.RightEdge
        if point.y() < self.margin:
            edges |= Qt.TopEdge
        if point.y() >= w.height() - self.margin:
            edges |= Qt.BottomEdge
        return edges

    def eventFilter(self, target, event):
        if not isinstance(target, QWidget) or target.window() is not self.window:
            return False
        if event.type() not in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            return False
        if event.button() != Qt.LeftButton:
            return False
        position = self.window.mapFromGlobal(event.globalPosition().toPoint())
        edges = self.edges(position)
        handle = self.window.windowHandle()
        if edges and handle and event.type() == QEvent.MouseButtonPress:
            return handle.startSystemResize(edges)
        if (
            target is self.bar
            or isinstance(target, QLabel)
            and self.bar.isAncestorOf(target)
        ):
            if event.type() == QEvent.MouseButtonDblClick:
                (
                    self.window.showNormal()
                    if self.window.isMaximized()
                    else self.window.showMaximized()
                )
                return True
            if handle:
                return handle.startSystemMove()
        return False


def build_titlebar(window):
    window.setWindowFlag(Qt.FramelessWindowHint, True)
    bar = QFrame()
    bar.setObjectName("topbar")
    bar.setFixedHeight(48)
    layout = hbox(bar, spacing=12, margins=(16, 0, 4, 0))
    title = QLabel("LSM测量工作站")
    title.setObjectName("brand")
    layout.addWidget(title)
    version = QLabel("2.8")
    version.setObjectName("muted")
    layout.addWidget(version)
    window.conn_chip = StatusText("未连接")
    window.mode_chip = StatusText("真实设备")
    window.xy_chip = StatusText(
        "有电动 XY" if window.xy_mode == "motorized" else "无 XY · 单点"
    )
    window.recovery_chip = StatusText("无待恢复项")
    for item in (
        window.conn_chip,
        window.mode_chip,
        window.xy_chip,
        window.recovery_chip,
    ):
        item.set_kind(None)
        layout.addWidget(item)
    layout.addStretch(1)
    window.read_labels = {}
    for key in ("X", "Y", "Z", "物镜"):
        layout.addWidget(QLabel(key))
        value = QLabel("—")
        value.setMinimumWidth(35)
        value.setStyleSheet("font-family:Consolas; font-weight:600;")
        value.setToolTip("设备读回位置 · XYZ 单位 µm；物镜为转盘位置，仅只读")
        window.read_labels[key] = value
        layout.addWidget(value)
    units = QLabel("µm")
    units.setObjectName("muted")
    layout.addWidget(units)
    window.stop_button = QPushButton("■ 停止")
    window.stop_button.setObjectName("danger")
    window.stop_button.setShortcut("Esc")
    window.stop_button.clicked.connect(window.stop)
    layout.addWidget(window.stop_button)
    for text, tip, fn in [
        ("−", "最小化", window.showMinimized),
        (
            "□",
            "最大化 / 还原",
            lambda: (
                window.showNormal() if window.isMaximized() else window.showMaximized()
            ),
        ),
        ("×", "关闭", window.close),
    ]:
        button = QPushButton(text)
        button.setToolTip(tip)
        button.setFixedSize(32, 32)
        button.setStyleSheet(
            "border:none; border-radius:4px; font-size:17px; padding:0;"
        )
        button.clicked.connect(fn)
        layout.addWidget(button)
    window.chrome = WindowChrome(window, bar)
    return bar
