"""硬件单步调试 page: OPERATIONS catalogue, dynamic forms, durable session log."""

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTreeWidget,
    QTreeWidgetItem, QWidget, QApplication,
)

from zen_hardware.library import OPERATIONS, HardwareTestSession
from zen_records import pending_recovery
from .. import theme
from ..widgets import Card, Pill, hbox, label, note, vbox
from .device import _page


class HardwarePage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.op = None
        self.values = {}
        self.latest = None
        lay, head = _page(self, "硬件单步调试",
                          "按目录逐项调用 Gateway 接口 · 写入前要求人工确认 · 全部调用记入持久日志")

        split = QSplitter()
        lay.addWidget(split, 1)

        left = Card("操作目录")
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemSelectionChanged.connect(self._select)
        left.add(self.tree)
        split.addWidget(left)

        right = QWidget()
        rl = vbox(right, spacing=11, margins=0)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)

        self.bind_pill = Pill("未绑定")
        self.bind_card = Card("测试会话", trailing=self.bind_pill)
        self.bind_info = label("", role="muted", wrap=True)
        self.bind_card.add(self.bind_info)
        bind_row = QWidget()
        bl = hbox(bind_row, spacing=8, margins=0)
        bl.addWidget(QPushButton("绑定当前设备", clicked=self.bind_current))
        bl.addWidget(QPushButton("新建会话日志", clicked=self.new_session))
        bl.addStretch(1)
        self.bind_card.add(bind_row)
        rl.addWidget(self.bind_card)

        self.op_kind = Pill("只读")
        self.op_card = Card("操作", trailing=self.op_kind)
        self.op_title = label("请在左侧选择一个具体操作（分组条目不可执行）。", role="muted", wrap=True)
        self.op_card.add(self.op_title)
        self.help_label = label("", role="muted", wrap=True)
        self.help_label.setStyleSheet("font-size:11.5px")
        self.op_card.add(self.help_label)
        self.form = QFormLayout()
        self.form.setSpacing(7)
        self.op_card.add_layout(self.form)
        row = QWidget()
        rowl = hbox(row, spacing=8, margins=0)
        rowl.addWidget(label("RPC 超时 (s)", role="muted"))
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(1, 600)
        self.timeout.setValue(60)
        rowl.addWidget(self.timeout)
        self.approved = QCheckBox("我已知晓此操作会写入参数或移动设备")
        rowl.addWidget(self.approved, 1)
        self.run_button = QPushButton("执行", clicked=self.run)
        self.run_button.setObjectName("primary")
        rowl.addWidget(self.run_button)
        self.stop_button = QPushButton("请求停止", clicked=self.request_stop)
        self.stop_button.setStyleSheet(f"color:{theme.BAD}; border-color:#e2a59d;")
        rowl.addWidget(self.stop_button)
        self.op_card.add(row)
        rl.addWidget(self.op_card)

        self.result_pill = Pill("—")
        result_card = Card("结果与日志", trailing=self.result_pill)
        self.result_info = label("", role="muted", wrap=True)
        result_card.add(self.result_info)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        result_card.add(self.log_view)
        buttons = QWidget()
        btl = hbox(buttons, spacing=6, margins=0)
        for text, fn in (("复制最新", self.copy_latest), ("复制全部", self.copy_all),
                         ("导出日志…", self.export), ("恢复确认…", self.app.open_recovery)):
            btl.addWidget(QPushButton(text, clicked=fn))
        btl.addStretch(1)
        result_card.add(buttons)
        rl.addWidget(result_card, 1)

        self._populate_tree()
        self.session = HardwareTestSession(app.data_root)
        self._update_bind_info()

    # ---- session / binding ----

    def new_session(self):
        self.session = HardwareTestSession(self.app.data_root)
        self._update_bind_info()
        self.app.log("info", f"已新建硬件测试会话：{self.session.directory}")

    def bind_current(self):
        workflow = self.app.workflow
        if workflow is None:
            self.bind_info.setText("尚无设备；请先在「设备与自检」连接或载入离线演示。")
            return
        if workflow.busy:
            return
        try:
            self.session.bind(workflow.device, simulated=workflow.simulated)
        except RuntimeError as exc:
            self.bind_info.setText(str(exc))
            return
        self._update_bind_info()
        self.bind_info.setText("已绑定当前设备；单步调用将与测量任务串行执行。")

    def _update_bind_info(self):
        bound = self.session.library is not None
        self.bind_pill.set_kind("ok" if bound else "mute", "已绑定" if bound else "未绑定")
        if not bound:
            self.bind_info.setText(
                f"会话目录 {self.session.directory}\n绑定后单步调用与测量共用同一设备通道。")

    def on_show(self):
        workflow = self.app.workflow
        if (workflow is not None and self.session.library is not None
                and self.session.library.device is not workflow.device):
            self.bind_pill.set_kind("warn", "设备已变更")
            self.bind_info.setText("当前设备已更换，请重新「绑定当前设备」。")

    # ---- operation tree / form ----

    def _populate_tree(self):
        groups = {}
        for key, op in OPERATIONS.items():
            groups.setdefault(op.group, []).append(op)
        self.tree.clear()
        for group, ops in groups.items():
            parent = QTreeWidgetItem([group])
            parent.setFlags(parent.flags() & ~Qt.ItemIsSelectable)
            self.tree.addTopLevelItem(parent)
            for op in ops:
                child = QTreeWidgetItem([op.title])
                child.setData(0, Qt.UserRole, op.key)
                parent.addChild(child)
            parent.setExpanded(True)

    def _select(self):
        items = self.tree.selectedItems()
        self.op = None
        while self.form.rowCount():
            self.form.removeRow(0)
        self.values = {}
        self.approved.setChecked(False)
        if not items:
            return
        key = items[0].data(0, Qt.UserRole)
        if key is None or key not in OPERATIONS:
            self.op_title.setText("请在左侧选择一个具体操作（分组条目不可执行）。")
            self.help_label.setText("")
            return
        self.op = OPERATIONS[key]
        self.op_title.setText(self.op.title)
        self.op_title.setStyleSheet("font-weight:600")
        self.help_label.setText(self.op.help)
        kind = ("stop" if self.op.is_stop else "moving" if self.op.may_move
                else "write" if self.op.writes else "read")
        kind_map = {"read": ("mute", "只读"), "write": ("warn", "写入"),
                    "moving": ("warn", "会运动"), "stop": ("bad", "停止请求")}
        k, text = kind_map[kind]
        self.op_kind.set_kind(k, text)
        for field in self.op.fields:
            entry = QLineEdit("10" if field.name == "af_timeout_s" else "")
            if field.optional:
                entry.setPlaceholderText("可选")
            self.values[field.name] = entry
            self.form.addRow(field.label, entry)

    def run(self):
        workflow = self.app.workflow
        if workflow is None or workflow.busy or self.session.library is None:
            self.result_info.setText("请先绑定设备，或等待当前任务结束。")
            return
        if self.op is None:
            self.result_info.setText("请先在左侧选择一个具体操作。")
            return
        if self.op.xy_required and not workflow.xy_enabled:
            self.result_info.setText("此操作需要电动 XY；当前为无 XY 连接。")
            return
        timeout = float(self.timeout.value())
        params = {name: entry.text() for name, entry in self.values.items()}
        confirmed = self.approved.isChecked()
        self.approved.setChecked(False)
        self.result_info.setText(f"正在执行 {self.op.title}；同步 RPC 可能持续至超时。")
        self.run_button.setEnabled(False)
        try:
            workflow.run_hardware_test(self.session, self.op.key, params,
                                       confirmed=confirmed, timeout_s=timeout)
        except Exception as exc:  # noqa: BLE001 — surface as result text
            self.run_button.setEnabled(True)
            self.result_info.setText("未能提交单步：" + str(exc))

    def request_stop(self):
        if self.app.workflow:
            self.app.workflow.stop()
        self.session.note_stop_request()
        self.result_info.setText("已请求停止后续步骤；不能取消正在等待的同步 RPC，请在设备端确认停止。")

    def on_workflow_event(self, kind, data, workflow):
        if kind != "hardware_result":
            return
        report = data[0]
        self.latest = report
        self.run_button.setEnabled(True)
        status = report.get("status", "?")
        self.result_pill.set_kind("ok" if status == "success" else "bad",
                                  status + (" · 须人工确认" if report.get("recovery_required") else ""))
        self.result_info.setText(
            f"{report.get('title', report.get('operation'))} · {status}"
            + ("；设备状态须人工确认" if report.get("recovery_required") else "；详见日志"))
        self.log_view.appendPlainText(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if report["operation"] in ("connect", "disconnect") and "experiment_id" in self.values:
            self.values["experiment_id"].setText("")
        result = report.get("result") or {}
        if status == "success" and report["operation"] in ("load_experiment", "clone_experiment") \
                and result.get("experiment_id") and "experiment_id" in self.values:
            self.values["experiment_id"].setText(result["experiment_id"])
        if report.get("writes"):
            self.app.field_verified = False

    def copy_latest(self):
        if self.latest:
            QApplication.clipboard().setText(json.dumps(self.latest, ensure_ascii=False, indent=2))

    def copy_all(self):
        if self.session.log_path.exists():
            QApplication.clipboard().setText(self.session.log_path.read_text(encoding="utf-8"))

    def export(self):
        if not self.session.log_path.exists():
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", "hardware-test.log", "Log (*.log)")
        if not path:
            return
        try:
            Path(path).write_text(self.session.log_path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def refresh_enabled(self):
        busy = bool(self.app.workflow and self.app.workflow.busy)
        can_run = bool(self.app.workflow and not busy and self.session.library is not None
                       and self.op is not None)
        self.run_button.setEnabled(self.run_button.isEnabled() and not busy or can_run)
        self.stop_button.setEnabled(busy)
