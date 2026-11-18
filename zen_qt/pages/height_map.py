"""Height map controls and views; never displays filled pixels as measured data."""

import numpy as np

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QComboBox,
    QSpinBox,
    QDoubleSpinBox,
    QPushButton,
    QLabel,
)
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from zen_topography import HeightParameters


class HeightMapView(QWidget):
    def __init__(self, on_change, on_generate, on_export, on_stop):
        super().__init__()
        layout = QVBoxLayout(self)
        self.method = QComboBox()
        for title, value in [
            ("共聚焦强度 · 对数高斯", "gaussian"),
            ("共聚焦强度 · 三点抛物线", "parabolic"),
            ("共聚焦强度 · 离散峰值", "peak"),
            ("清晰度 · 对数高斯", "clarity"),
        ]:
            self.method.addItem(title, value)
        self.grid = QSpinBox()
        self.grid.setRange(1, 1024)
        self.grid.setValue(8)
        self.grid.setSuffix(" px")
        controls = QHBoxLayout()
        controls.addWidget(QLabel("算法"))
        controls.addWidget(self.method, 1)
        controls.addWidget(QLabel("采样网格"))
        controls.addWidget(self.grid)
        self.generate = QPushButton("生成高度图", clicked=on_generate)
        self.export = QPushButton("导出高度图…", clicked=on_export)
        self.stop = QPushButton("取消计算", clicked=on_stop)
        for b in (self.generate, self.export, self.stop):
            controls.addWidget(b)
        layout.addLayout(controls)
        quality = QHBoxLayout()
        self.fields = {}
        for name, title, value, maximum in [
            ("signal_fraction", "信号门限", 0.08, 1),
            ("r2_min", "R²门限", 0, 1),
            ("residual_sigma", "残差σ", 3, 20),
            ("spike_sigma", "尖刺σ（0关闭）", 0, 20),
        ]:
            spin = QDoubleSpinBox()
            spin.setDecimals(3)
            spin.setRange(0, maximum)
            spin.setValue(value)
            spin.valueChanged.connect(on_change)
            quality.addWidget(QLabel(title))
            quality.addWidget(spin)
            self.fields[name] = spin
        layout.addLayout(quality)
        self.summary = QLabel("读取堆栈后点击“生成高度图”。空白为无效数据，不作补值。")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.figure = Figure(figsize=(8, 3), layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        layout.addWidget(self.canvas, 1)
        self.method.currentIndexChanged.connect(on_change)
        self.grid.valueChanged.connect(on_change)
        self.generate.setEnabled(False)
        self.export.setEnabled(False)
        self.stop.setEnabled(False)
        self.clear()

    def parameters(self):
        return HeightParameters(
            method=self.method.currentData(),
            grid_px=self.grid.value(),
            **{k: s.value() for k, s in self.fields.items()},
        )

    def set_state(self, *, busy, has_stack, has_result, calculating=False):
        for item in (self.method, self.grid, *self.fields.values()):
            item.setEnabled(not busy)
        self.generate.setEnabled(not busy and has_stack)
        self.export.setEnabled(not busy and has_result)
        self.stop.setEnabled(calculating)

    def clear(self):
        self.figure.clear()
        self.canvas.draw_idle()
        self.summary.setText(
            "尚无本轮高度图。无效点保持空白；取消、失败或参数改变后不沿用旧结果。"
        )

    def show_result(self, result):
        self.figure.clear()
        x, y = result["x_um"], result["y_um"]
        dx = x[1] - x[0]
        dy = y[1] - y[0]
        extent = [x[0] - dx / 2, x[-1] + dx / 2, y[-1] + dy / 2, y[0] - dy / 2]
        for i, key, title in [
            (1, "height_um", "Height (um)"),
            (2, "residual_um", "Plane residual (um)"),
        ]:
            ax = self.figure.add_subplot(1, 2, i)
            values = (
                result[key]
                if i == 1
                else np.where(result["inlier_mask"], result[key], float("nan"))
            )
            im = ax.imshow(
                values,
                origin="upper",
                extent=extent,
                interpolation="nearest",
                aspect="equal",
                cmap="viridis" if i == 1 else "coolwarm",
            )
            ax.set_title(title)
            ax.set_xlabel("Local X (um)")
            ax.set_ylabel("Local Y (um)")
            self.figure.colorbar(im, ax=ax)
        self.canvas.draw_idle()
        s = result["statistics"]
        prefix = "模拟数据 · " if result["simulation"] else ""
        self.summary.setText(
            f"{prefix}高度有效 {s['valid_count']}/{s['total_count']}；平面内点 {s['plane_inlier_count']}；边界峰 {s['boundary_count']}\n"
            f"Rx {s['rx_urad']:.3f} µrad · Ry {s['ry_urad']:.3f} µrad · PV {s['pv_um']:.4f} µm · RMS {s['rms_um']:.4f} µm · TTV {s['ttv_um']:.4f} µm\n"
            "局部图像坐标结果，未标定到载台轴；PV/RMS为去平面后残差，TTV为平面内点的原始高度范围。"
        )
