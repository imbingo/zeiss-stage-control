"""Pick checks and edit per-operation parameters before a single test run."""

from copy import deepcopy
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QCheckBox,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QHeaderView,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
)
from zen_hardware.library import OPERATIONS


class BatchDialog(QDialog):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setWindowTitle("选择硬件一键测试项目")
        self.resize(960, 700)
        lay = QVBoxLayout(self)
        lay.addWidget(
            QLabel(
                "仅执行勾选项目；双击参数列编辑。未勾选不调用；失败记录后继续检查。状态不明确时跳过新写入。"
            )
        )
        self.xy = QCheckBox("本机有电动 XY（无 XY 时取消；不会调用 XY 接口）")
        self.xy.setChecked(page.app.xy_mode == "motorized")
        lay.addWidget(self.xy)
        self.table = QTableWidget(len(OPERATIONS), 3)
        self.table.setHorizontalHeaderLabels(
            ["项目（勾选执行）", "参数（双击编辑）", "说明"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        # Parameters and help share the remaining width; full text is in the tooltip.
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.plan = []
        for i, (key, op) in enumerate(OPERATIONS.items()):
            params = {
                f.name: (
                    ("@" + f.name)
                    if f.name in ("experiment_id", "camera_id")
                    else page.field_values.get(f.name, "")
                )
                for f in op.fields
            }
            row = {
                "operation": key,
                "selected": key
                in (
                    "connect",
                    "list_services",
                    "read_z",
                    "read_optics",
                    "list_experiments",
                    "list_cameras",
                ),
                "parameters": params,
            }
            self.plan.append(row)
            item = QTableWidgetItem(op.title)
            item.setFlags((item.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
            item.setCheckState(Qt.Checked if row["selected"] else Qt.Unchecked)
            self.table.setItem(i, 0, item)
            for column, text in [
                (1, ""),
                (2, op.help or ("只读查询" if not op.writes else "修改参数/硬件")),
            ]:
                cell = QTableWidgetItem(text)
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                cell.setToolTip(text)
                self.table.setItem(i, column, cell)
            self.refresh_row(i)
        self.table.cellDoubleClicked.connect(self.edit)
        self.table.itemChanged.connect(
            lambda item: item.column() == 0 and self.refresh_row(item.row())
        )
        lay.addWidget(self.table, 1)
        controls = QHBoxLayout()
        self.zero_z = QCheckBox("结束后 Z → 绝对坐标 0 µm")
        self.zero_xy = QCheckBox("结束后 XY → 绝对坐标 0 µm")
        self.zero_xy.setEnabled(self.xy.isChecked())
        self.xy.toggled.connect(self._xy_toggled)
        controls.addWidget(self.zero_z)
        controls.addWidget(self.zero_xy)
        lay.addLayout(controls)
        self.zero_hint = QLabel()
        self.zero_hint.setWordWrap(True)
        lay.addWidget(self.zero_hint)
        self.empty = QCheckBox(
            "我已确认载台空载、无物料，已核对各项参数及绝对零坐标的运动路径；该按钮不是急停。"
        )
        lay.addWidget(self.empty)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        for box in (self.zero_z, self.zero_xy):
            box.toggled.connect(self.refresh_zero_hint)
        self.refresh_zero_hint()
        self.buttons.accepted.connect(self.start)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

    def _xy_toggled(self, enabled):
        self.zero_xy.setEnabled(enabled)
        if not enabled:
            self.zero_xy.setChecked(False)
        self.refresh_zero_hint()

    def zero_moves(self):
        """Axes that will be driven to absolute 0 after the checked items."""
        moves = []
        if self.zero_z.isChecked():
            moves.append("Z")
        if self.xy.isChecked() and self.zero_xy.isChecked():
            moves.append("XY")
        return moves

    def refresh_zero_hint(self, *_):
        moves = self.zero_moves()
        ok = self.buttons.button(QDialogButtonBox.Ok)
        if not moves:
            self.zero_hint.setText("结束后不移动任何轴；需要回到绝对 0 µm 时再勾选上面的选项。")
            self.zero_hint.setStyleSheet("")
            ok.setText("开始勾选项目测试")
            return
        app = self.page.app
        parts = []
        if "Z" in moves:
            z = getattr(app, "_live_z", None)
            parts.append(
                f"Z：{z * 1e6:.3f} → 0 µm（移动 {-z * 1e6:+.3f} µm）"
                if z is not None
                else "Z：当前位置未知 → 0 µm"
            )
        if "XY" in moves:
            xy = getattr(app, "_live_xy", None)
            parts.append(
                f"XY：({xy[0] * 1e6:.3f}, {xy[1] * 1e6:.3f}) → (0, 0) µm"
                if xy is not None
                else "XY：当前位置未知 → (0, 0) µm"
            )
        self.zero_hint.setText(
            "测试结束后设备会运动（只勾只读项目时也会执行）：" + "；".join(parts)
        )
        self.zero_hint.setStyleSheet("color:#b45309;font-weight:600;")
        ok.setText("开始测试（结束后移动 " + " / ".join(moves) + " 到 0 µm）")

    @staticmethod
    def summary(params):
        return "；".join(f"{k}={v}" for k, v in params.items()) or "无参数"

    def missing(self, row):
        """Labels of required parameters that are still empty for this row."""
        item = self.plan[row]
        return [
            f.label
            for f in OPERATIONS[item["operation"]].fields
            if not f.optional and not str(item["parameters"].get(f.name, "")).strip()
        ]

    def refresh_row(self, row):
        """Checked rows with empty required parameters must stand out before start."""
        item, cell = self.plan[row], self.table.item(row, 1)
        if cell is None:
            return
        missing = self.missing(row)
        checked = self.table.item(row, 0).checkState() == Qt.Checked
        text = "；".join(
            f"{f.label} = {item['parameters'].get(f.name, '') or '（空）'}"
            for f in OPERATIONS[item["operation"]].fields
        ) or "无参数"
        if missing:
            text = ("缺少必填：" if checked else "勾选前需填写：") + "、".join(missing) + "（双击填写）"
        cell.setText(text)
        cell.setToolTip(text)
        cell.setForeground(
            QColor("#bf3a2b" if missing and checked else "#6a7584" if missing else "#18222f")
        )

    def edit(self, row, column):
        if column != 1:
            return
        item = self.plan[row]
        op = OPERATIONS[item["operation"]]
        if not op.fields:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(op.title)
        form = QFormLayout(dialog)
        edits = {}
        for field in op.fields:
            edit = QLineEdit(str(item["parameters"].get(field.name, "")))
            edit.setPlaceholderText("可留空" if field.optional else "必填")
            form.addRow(field.label + ("" if field.optional else " *"), edit)
            edits[field.name] = edit
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.Accepted:
            item["parameters"] = {k: e.text() for k, e in edits.items()}
            self.refresh_row(row)

    def start(self):
        if not self.empty.isChecked():
            QMessageBox.information(
                self, "测试前检查", "请先核对空载、参数与绝对零坐标路径。"
            )
            return
        for i, row in enumerate(self.plan):
            row["selected"] = self.table.item(i, 0).checkState() == Qt.Checked
        if (
            not any(r["selected"] for r in self.plan)
            and not self.zero_z.isChecked()
            and not (self.xy.isChecked() and self.zero_xy.isChecked())
        ):
            QMessageBox.information(self, "未选择项目", "请选择测试项目。")
            return
        gaps = [
            f"{OPERATIONS[r['operation']].title}：{'、'.join(self.missing(i))}"
            for i, r in enumerate(self.plan)
            if r["selected"] and self.missing(i)
        ]
        if gaps:
            first = next(i for i, r in enumerate(self.plan) if r["selected"] and self.missing(i))
            self.table.selectRow(first)
            self.table.scrollToItem(self.table.item(first, 1))
            QMessageBox.information(
                self,
                "缺少必填参数",
                "以下已勾选项目缺少必填参数，双击“参数”列填写，或取消勾选：\n"
                + "\n".join(gaps),
            )
            return
        w = self.page.app.workflow
        if self.xy.isChecked() != w.xy_enabled and not any(
            r["selected"] and r["operation"] == "connect" for r in self.plan
        ):
            QMessageBox.information(
                self,
                "轴配置变化",
                "改变轴配置时请同时勾选“连接通信”，以重新初始化对应接口。",
            )
            return
        self.accept()
