"""Pick checks and edit per-operation parameters before a single test run."""

from copy import deepcopy
from PySide6.QtCore import Qt
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
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
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
                (1, self.summary(params)),
                (2, op.help or ("只读查询" if not op.writes else "修改参数/硬件")),
            ]:
                cell = QTableWidgetItem(text)
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                cell.setToolTip(text)
                self.table.setItem(i, column, cell)
        self.table.cellDoubleClicked.connect(self.edit)
        lay.addWidget(self.table, 1)
        controls = QHBoxLayout()
        self.zero_z = QCheckBox("结束后 Z → 绝对坐标 0 µm")
        self.zero_z.setChecked(True)
        self.zero_xy = QCheckBox("结束后 XY → 绝对坐标 0 µm")
        self.zero_xy.setChecked(self.xy.isChecked())
        self.zero_xy.setEnabled(self.xy.isChecked())
        self.xy.toggled.connect(self.zero_xy.setEnabled)
        controls.addWidget(self.zero_z)
        controls.addWidget(self.zero_xy)
        lay.addLayout(controls)
        self.empty = QCheckBox(
            "我已确认载台空载、无物料，已核对各项参数及绝对零坐标的运动路径；该按钮不是急停。"
        )
        lay.addWidget(self.empty)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("开始勾选项目测试")
        self.buttons.accepted.connect(self.start)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

    @staticmethod
    def summary(params):
        return "；".join(f"{k}={v}" for k, v in params.items()) or "无参数"

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
            form.addRow(field.label, edit)
            edits[field.name] = edit
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.Accepted:
            item["parameters"] = {k: e.text() for k, e in edits.items()}
            self.table.item(row, 1).setText(self.summary(item["parameters"]))

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
