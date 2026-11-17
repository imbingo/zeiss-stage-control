"""Preview-first path generation. Read buttons only issue guarded position queries."""

from uuid import uuid4
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QComboBox,
    QPushButton,
    QLabel,
    QDialogButtonBox,
    QHBoxLayout,
)
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from zen_scan_path import generate_path


class PathDialog(QDialog):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.token = uuid4().hex
        self.workflow = None
        self.points = None
        self.setWindowTitle("批量生成路径 · 设备绝对坐标 µm")
        self.resize(720, 650)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItem("矩形网格 · 蛇形顺序", "grid")
        self.kind.addItem("直线均布 · 不能单独用于平面测角", "line")
        form.addRow("路径类型", self.kind)
        self.coords = [QLineEdit() for _ in range(4)]
        for text, edit in zip(
            ("起点 X · µm", "起点 Y · µm", "终点 X · µm", "终点 Y · µm"), self.coords
        ):
            edit.setPlaceholderText("设备绝对坐标，或使用下方只读示教")
            form.addRow(text, edit)
        read_row = QHBoxLayout()
        for text, offset in (("读取当前 XY 为起点", 0), ("读取当前 XY 为终点", 2)):
            button = QPushButton(text)
            button.clicked.connect(
                lambda checked=False, offset=offset: self.read_endpoint(offset)
            )
            read_row.addWidget(button)
        form.addRow(read_row)
        self.rows, self.columns, self.count = QSpinBox(), QSpinBox(), QSpinBox()
        for control, value in ((self.rows, 4), (self.columns, 5), (self.count, 20)):
            control.setRange(2, 1000)
            control.setValue(value)
        form.addRow("网格行数", self.rows)
        form.addRow("网格列数", self.columns)
        form.addRow("直线点数", self.count)
        self.apply_mode = QComboBox()
        self.apply_mode.addItems(["替换现有路径", "追加到路径末尾"])
        form.addRow("应用方式", self.apply_mode)
        layout.addLayout(form)
        self.figure = Figure(figsize=(5, 2))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumHeight(160)
        layout.addWidget(self.canvas, 1)
        self.notice = QLabel(
            "先填写坐标并预览；所有操作都不会移动设备。应用后需重新选择四个预扫描点。"
        )
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("应用预览路径（不运动）")
        self.buttons.accepted.connect(self.apply)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        for control in self.coords:
            control.textChanged.connect(self.preview)
        for control in (self.rows, self.columns, self.count):
            control.valueChanged.connect(self.preview)
        self.kind.currentIndexChanged.connect(self.preview)
        self.preview()

    def read_endpoint(self, offset):
        w = self.page.app.workflow
        if not w or not w.xy_enabled:
            self.notice.setText(
                "需要已连接的电动 XY；可以手工输入坐标，不发送任何 XY 请求。"
            )
            return
        if w.busy:
            self.notice.setText("设备任务正在进行，请等待结束后重新点击只读示教。")
            return
        self.workflow = w
        generation = getattr(w, "action_generation", 0)
        w.read_only(
            "path_endpoint",
            lambda d: dict(
                token=self.token, offset=offset, generation=generation, xy=d.read_xy()
            ),
        )

    def on_result(self, value):
        w = self.page.app.workflow
        if (
            not self.isVisible()
            or value["token"] != self.token
            or w is not self.workflow
            or value["generation"] != getattr(w, "action_generation", 0)
        ):
            return
        for edit, v in zip(
            self.coords[value["offset"] : value["offset"] + 2], value["xy"]
        ):
            edit.setText(f"{v*1e6:.6f}")

    def preview(self, *_):
        grid = self.kind.currentData() == "grid"
        self.rows.setEnabled(grid)
        self.columns.setEnabled(grid)
        self.count.setEnabled(not grid)
        self.points = None
        self.figure.clear()
        try:
            self.points = generate_path(
                *(float(e.text()) for e in self.coords),
                kind=self.kind.currentData(),
                rows=self.rows.value(),
                columns=self.columns.value(),
                count=self.count.value(),
            )
            x, y = zip(*[p[:2] for p in self.points])
            ax = self.figure.add_subplot(111)
            ax.plot(x, y, "o-", markersize=3)
            ax.set_xlabel("X · µm")
            ax.set_ylabel("Y · µm")
            ax.set_aspect("equal", adjustable="box")
            for i in sorted(set((0, len(x) - 1))):
                ax.annotate(str(i + 1), (x[i], y[i]))
            self.figure.tight_layout()
            spacing = (
                f"网格间距 X {abs(float(self.coords[2].text())-float(self.coords[0].text()))/(self.columns.value()-1):g} / Y {abs(float(self.coords[3].text())-float(self.coords[1].text()))/(self.rows.value()-1):g} µm"
                if grid
                else f"点间距 {((x[-1]-x[0])**2+(y[-1]-y[0])**2)**.5/(len(x)-1):g} µm"
            )
            self.notice.setText(
                f"{len(x)} 点；{spacing}；X 跨度 {max(x)-min(x):g} µm；Y 跨度 {max(y)-min(y):g} µm。连线为采集顺序，实际转场仍使用安全 Z。应用会清除原四点选择。"
            )
        except ValueError as e:
            self.notice.setText(str(e) or "请填写四个坐标")
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(self.points is not None)
        self.canvas.draw_idle()

    def apply(self):
        if self.points is not None:
            self.accept()
