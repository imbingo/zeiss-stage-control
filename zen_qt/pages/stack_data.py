"""Device-independent offline Z-stack metadata verification and algorithm handoff."""

from pathlib import Path
from uuid import uuid4
from threading import Thread

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QFileDialog,
    QFormLayout,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
)
from .. import worker
from zen_stack_data import load_stack, export_stack


class StackDataDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Z-stack 数据 · 图像与逐层 Z")
        self.resize(900, 600)
        self.stack = None
        self.source_path = None
        self.busy = False
        self.tag = "stack-data-" + uuid4().hex
        layout = QVBoxLayout(self)
        text = QLabel(
            "离线读取，不连接或控制设备。支持 OME-XML + TIFF、内嵌 OME-TIFF，以及工作站 stack.xml。\n"
            "读取采集元数据的逐层 Z；不使用实验起始 Z + 步进补值。尚不支持任意 CZI 原生 XML。"
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        form = QFormLayout()
        self.indices = []
        for label in (
            "Image 索引（从 0 起）",
            "通道索引（从 0 起）",
            "时间点索引（从 0 起）",
        ):
            spin = QSpinBox()
            spin.setRange(0, 100000)
            spin.valueChanged.connect(self.invalidate)
            form.addRow(label, spin)
            self.indices.append(spin)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.open_button = QPushButton("选择 XML / OME-TIFF…", clicked=self.choose)
        self.export_button = QPushButton("导出算法输入…", clicked=self.choose_export)
        self.export_button.setEnabled(False)
        self.close_button = QPushButton("关闭", clicked=self.reject)
        for button in (self.open_button, self.export_button, self.close_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.status = QLabel("请选择配套 XML 或 OME-TIFF。未执行高度图计算。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Z 层号", "Z (µm)", "TIFF 页号（从 0 起）", "图像文件"]
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        foot = QLabel(
            "导出保留原始 uint8/uint16 图像，生成图像数组 ZYX、逐层 Z 数组（µm）、TIFF+XML 和来源指纹。\n"
            "元数据 Z 不等于独立编码器实测；高度图需由已验证的外部算法处理。"
        )
        foot.setWordWrap(True)
        layout.addWidget(foot)
        self.bridge = worker.CallBridge(self)
        bridge = self.bridge
        bridge.done.connect(self.done_result)
        bridge.fail.connect(self.failed)
        self.finished.connect(self.disconnect_bridge)

    def disconnect_bridge(self, *_):
        b = self.bridge
        b.done.disconnect(self.done_result)
        b.fail.disconnect(self.failed)

    def launch(self, tag, fn):
        bridge = self.bridge

        def run():
            try:
                value = fn()
            except Exception as error:
                bridge.fail.emit(tag, error)
            else:
                bridge.done.emit(tag, value)

        Thread(target=run, daemon=True).start()

    def invalidate(self, *_):
        self.stack = None
        self.table.setRowCount(0)
        self.export_button.setEnabled(False)
        self.status.setText("选择已改变，请重新选择源文件并读取。")

    def set_busy(self, busy):
        self.busy = busy
        self.open_button.setEnabled(not busy)
        self.export_button.setEnabled(not busy and self.stack is not None)
        self.close_button.setEnabled(not busy)
        for spin in self.indices:
            spin.setEnabled(not busy)

    def reject(self):
        if not self.busy:
            super().reject()

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
        else:
            super().closeEvent(event)

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图像元数据", "", "Z-stack (*.xml *.tif *.tiff)"
        )
        if path:
            self.read_path(path)

    def read_path(self, path):
        if self.busy:
            return
        self.invalidate()
        self.source_path = str(path)
        image, channel, time = [spin.value() for spin in self.indices]
        self.set_busy(True)
        self.status.setText("正在校验图像、逐层 Z 和来源指纹…")
        self.launch(
            self.tag + ":read",
            lambda: load_stack(
                path, image_index=image, channel=channel, timepoint=time
            ),
        )

    def choose_export(self):
        path = QFileDialog.getExistingDirectory(
            self, "选择父目录；将在其下新建唯一结果目录"
        )
        if path:
            self.export_to(Path(path) / ("stack_" + uuid4().hex[:12]))

    def export_to(self, path):
        if self.busy or self.stack is None:
            return
        stack = self.stack
        self.set_busy(True)
        self.status.setText("正在保存算法输入；全部保存成功后才生成结果目录…")
        self.launch(self.tag + ":export", lambda: export_stack(stack, path))

    def done_result(self, tag, value):
        if not tag.startswith(self.tag + ":"):
            return
        if tag.endswith(":read"):
            self.stack = value
            self.table.setRowCount(len(value.planes))
            for row, p in enumerate(value.planes):
                for col, text in enumerate(
                    (p["index"], f"{p['z_um']:.9g}", p["page"], Path(p["file"]).name)
                ):
                    item = QTableWidgetItem(str(text))
                    item.setToolTip(str(p["file"]))
                    self.table.setItem(row, col, item)
            self.status.setText(
                f"数据校验通过 · {value.pixels.shape[0]} 层 · "
                f"{value.pixels.shape[2]} × {value.pixels.shape[1]} · {value.pixels.dtype}\n"
                f"Z 来源：{value.z_source}；本轮未计算高度图。"
            )
        else:
            self.status.setText(f"算法输入已保存：{value}\n未执行高度图计算。")
        self.set_busy(False)

    def failed(self, tag, error):
        if not tag.startswith(self.tag + ":"):
            return
        if tag.endswith(":read"):
            self.invalidate()
        self.status.setText(f"失败：{error}")
        self.set_busy(False)
