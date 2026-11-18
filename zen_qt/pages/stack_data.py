"""Device-independent offline Z-stack metadata verification and algorithm handoff."""

from pathlib import Path
from uuid import uuid4
from threading import Thread, Event

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QTabWidget,
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
        self.setWindowTitle("Z-stack · 数据与高度图")
        self.resize(900, 600)
        self.height_result = None
        self.analysis_stop = Event()
        self.operation = None
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
        self.tabs = QTabWidget()
        self.tabs.addTab(self.table, "逐层 Z 数据")
        from .height_map import HeightMapView

        self.height_view = HeightMapView(
            self.height_parameters_changed,
            self.generate_height,
            self.choose_height_export,
            self.cancel_height,
        )
        self.tabs.addTab(self.height_view, "高度图")
        layout.addWidget(self.tabs, 1)
        foot = QLabel(
            "导出保留原始 uint8/uint16 图像，生成图像数组 ZYX、逐层 Z 数组（µm）、TIFF+XML 和来源指纹。\n"
            "元数据 Z 不等于独立编码器实测；读取后可在“高度图”页生成并导出结果。"
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

    def height_parameters_changed(self, *_):
        self.height_result = None
        self.height_view.clear()
        self.height_view.set_state(
            busy=self.busy, has_stack=self.stack is not None, has_result=False
        )

    def generate_height(self):
        if self.busy or self.stack is None:
            return
        from zen_topography import build_height_map

        parameters = self.height_view.parameters()
        stack = self.stack
        self.height_parameters_changed()
        self.analysis_stop.clear()
        self.operation = "height"
        self.set_busy(True)
        self.status.setText("正在生成高度图，可取消；未连接设备…")
        self.launch(
            self.tag + ":height",
            lambda: build_height_map(stack, parameters, stop=self.analysis_stop),
        )

    def cancel_height(self):
        if self.operation == "height" and self.busy:
            self.analysis_stop.set()
            self.height_view.stop.setEnabled(False)
            self.status.setText("正在取消本地计算，等待当前计算段结束…")

    def choose_height_export(self):
        path = QFileDialog.getExistingDirectory(self, "选择高度图输出父目录")
        if path:
            self.export_height_to(Path(path) / ("height_" + uuid4().hex[:12]))

    def export_height_to(self, path):
        if self.busy or self.height_result is None:
            return
        from zen_topography import export_height_map

        result = self.height_result
        self.operation = "height_export"
        self.set_busy(True)
        self.launch(
            self.tag + ":height_export", lambda: export_height_map(result, path)
        )

    def invalidate(self, *_):
        self.stack = None
        self.height_parameters_changed()
        self.table.setRowCount(0)
        self.export_button.setEnabled(False)
        self.status.setText("选择已改变，请重新选择源文件并读取。")

    def set_busy(self, busy):
        self.busy = busy
        self.height_view.set_state(
            busy=busy,
            has_stack=self.stack is not None,
            has_result=self.height_result is not None,
            calculating=busy and self.operation == "height",
        )
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
        self.operation = "read"
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
        self.operation = "export"
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
            self.tabs.setCurrentWidget(self.height_view)
            self.status.setText(
                f"数据校验通过 · {value.pixels.shape[0]} 层 · "
                f"{value.pixels.shape[2]} × {value.pixels.shape[1]} · {value.pixels.dtype}\n"
                f"Z 来源：{value.z_source}；本轮未计算高度图。"
            )
        elif tag.endswith(":height"):
            if self.analysis_stop.is_set():
                self.height_result = None
                self.status.setText("高度图计算已取消，未产生本轮结果。")
            else:
                self.height_result = value
                self.height_view.show_result(value)
                self.tabs.setCurrentWidget(self.height_view)
                self.status.setText(
                    "高度图计算完成；可导出高度、有效掩膜、残差与来源记录。"
                )
        elif tag.endswith(":height_export"):
            self.status.setText(f"高度图已保存：{value}")
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
