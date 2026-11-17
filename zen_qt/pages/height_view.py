"""Shared runtime/archive view for real height scans, never derives missing values."""

from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QLabel,
    QPushButton,
    QFileDialog,
)
from ..widgets import vbox, hbox
from zen_heights import csv_fields
import shutil


class HeightView(QWidget):
    def __init__(self):
        super().__init__()
        layout = vbox(self, margins=0)
        self.selector = QComboBox()
        self.selector.currentIndexChanged.connect(self.render)
        layout.addWidget(self.selector)
        self.summary = QLabel("尚无本轮多面高度数据")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table, 1)
        self.note = QLabel(
            "单位 µm；差值为层间高度差，未扣物料厚度。空白表示未测，不是零。"
        )
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.export_button = QPushButton("另存本步骤 CSV…", clicked=self.export)
        layout.addWidget(self.export_button)
        self.clear()

    def clear(self):
        self.scans = {}
        self.selector.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.summary.setText("尚无本轮多面高度数据")
        self.export_button.setEnabled(False)

    def update_report(self, report):
        old = self.selector.currentData()
        self.scans = report.get("height_scans", {})
        if not self.scans:
            self.clear()
            return
        self.selector.blockSignals(True)
        self.selector.clear()
        for path, scan in self.scans.items():
            self.selector.addItem(path, path)
        self.selector.setCurrentIndex(max(0, self.selector.findData(old)))
        self.selector.blockSignals(False)
        self.render()

    def render(self, *_):
        scan = self.scans.get(self.selector.currentData())
        if not scan:
            return
        fields = csv_fields(scan["surface_count"])
        labels = {
            "point_no": "点号",
            "X_um": "X · µm",
            "Y_um": "Y · µm",
            "dZ21_um": "Z2−Z1 · µm",
            "dZ32_um": "Z3−Z2 · µm",
            "status": "状态",
        }
        names = {
            "running": "采集中",
            "partial": "部分完成",
            "stopped": "已停止",
            "complete": "已完成",
            "invalid_order": "面顺序异常",
        }
        rows = sorted(scan["rows"], key=lambda r: r["point_no"])
        self.table.setColumnCount(len(fields))
        self.table.setHorizontalHeaderLabels(
            [labels.get(k, k.replace("_um", " · µm")) for k in fields]
        )
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, key in enumerate(fields):
                value = row.get(key)
                text = (
                    ""
                    if value is None
                    else (
                        names.get(value, value)
                        if key == "status"
                        else f"{value:.3f}" if key != "point_no" else str(value)
                    )
                )
                cell = QTableWidgetItem(str(text))
                cell.setToolTip(
                    f"执行序号 {row['execution_index']} · 阶段 {row['phase']}"
                )
                self.table.setItem(i, j, cell)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        state = {
            "running": "采集中",
            "returning": "数据已采齐，正在返回安全 Z",
            "complete": "采集及安全 Z 返回完成",
            "failed": "任务失败",
            "stopped": "任务已停止",
        }.get(scan["state"], scan["state"])
        if (
            scan.get("data_complete")
            and not scan.get("return_complete")
            and scan["state"] == "failed"
        ):
            state = "数据已采齐，结束定位未完成"
        done = sum(r["status"] == "complete" for r in rows)
        self.summary.setText(
            ("离线演示 · 非现场数据\n" if scan["simulated"] else "")
            + f"{state} · 完成 {done} 点 · {scan['surface_count']} 面"
            + (" · XY 未自动核验" if not scan["xy_verified"] else "")
            + ("\n" + scan["error"] if scan.get("error") else "")
        )
        self.export_button.setEnabled(
            scan["state"] in ("complete", "failed", "stopped")
            and Path(scan["record_directory"], "heights.csv").is_file()
        )
        self.export_button.setToolTip(
            "扫描结束后另存 CSV；采集中不与归档写入并发读取"
            if scan["state"] in ("running", "returning")
            else "另存本步骤数据，保留归档原件"
        )

    def export(self):
        scan = self.scans.get(self.selector.currentData())
        if not scan:
            return
        file, _ = QFileDialog.getSaveFileName(
            self, "另存多面高度 CSV · µm", "heights.csv", "CSV (*.csv)"
        )
        if file:
            try:
                source = Path(scan["record_directory"]) / "heights.csv"
                if source.resolve() != Path(file).resolve():
                    shutil.copyfile(source, file)
            except OSError as e:
                self.note.setText("CSV 保存失败：" + str(e))
