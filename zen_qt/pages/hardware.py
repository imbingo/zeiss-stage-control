"""硬件单步调试 page: OPERATIONS catalogue, dynamic forms, durable session log."""

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHeaderView, QAbstractItemView, QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
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
        self.field_values = dict(app.preferences.data.get('hardware_test', {}))
        self.current_experiment_id = ''
        self._experiment_device = None
        lay, head = _page(self, "硬件单步调试",
                          "按目录逐项调用 Gateway 接口 · 写入前要求人工确认 · 全部调用记入持久日志")

        self.batch_button=QPushButton('一键测试…',clicked=self.start_batch)
        head.addWidget(QPushButton("Z-stack 数据…", clicked=self.open_stack_data))
        head.addWidget(self.batch_button)
        self.batch_report_button=QPushButton('导出一键测试报告…',clicked=self.export_batch)
        head.addWidget(self.batch_report_button)
        self.batch_report=None
        split = QSplitter()
        lay.addWidget(split, 1)

        left = Card("操作目录")
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setTextElideMode(Qt.ElideNone)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tree.header().setStretchLastSection(False)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.tree.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.tree.setMinimumWidth(240)
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
        self.timeout.setRange(1, 7200)
        try:
            self.timeout.setValue(float(self.field_values.get('rpc_timeout_s', '60')))
        except ValueError:
            self.timeout.setValue(60)
        self.timeout.setToolTip('按输入值执行，不自动延长；多层扫描请按实际时长增大。')
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
                         ("导出日志…", self.export), ("检查设备状态", self.app.open_recovery)):
            btl.addWidget(QPushButton(text, clicked=fn))
        btl.addStretch(1)
        result_card.add(buttons)
        rl.addWidget(result_card, 1)

        self._populate_tree()
        self.session = HardwareTestSession(app.data_root)
        self._update_bind_info()

    def open_stack_data(self):
        from .stack_data import StackDataDialog
        StackDataDialog(self).exec()

    # ---- session / binding ----

    def new_session(self):
        from ..action_feedback import defer_read
        if defer_read(self.app,self,self.new_session,lambda:id(self.app.workflow),self.result_info.setText):return
        bound = self.session.library.device if self.session.library else None
        self.session = HardwareTestSession(self.app.data_root)
        self.latest = None
        self.log_view.clear()
        self.result_pill.set_kind('mute', '—')
        if self.app.workflow and self.app.workflow.device is bound:
            self.session.bind(bound, simulated=self.app.workflow.simulated)
        self._update_bind_info()
        self.app.log("info", f"已新建硬件测试会话：{self.session.directory}")

    def bind_current(self):
        workflow = self.app.workflow
        if workflow is None:
            self.bind_info.setText("尚无设备；请先在「设备与自检」连接或载入离线演示。")
            return
        if workflow.busy:
            self.bind_info.setText('当前设备任务正在进行，请稍后绑定；不会新建另一条设备连接。');return
        if not hasattr(workflow.device, 'call_hardware_operation'):
            self.bind_info.setText('当前模拟器不支持硬件单步，请载入采图演示或连接现场设备。')
            return
        try:
            if self._experiment_device is not workflow.device:
                self.current_experiment_id = ''
            self._experiment_device = workflow.device
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

    def on_device_changed(self, workflow):
        self.current_experiment_id = ''
        self._experiment_device = None
        self.session.library = None
        self.approved.setChecked(False)
        self._update_bind_info()
        if self.op:
            self._select()

    def save_preferences(self):
        self.app.preferences.update(hardware_test={**self.field_values,
            'rpc_timeout_s': str(self.timeout.value())})

    def _edit(self, key, value):
        if key == 'experiment_id':
            self.current_experiment_id = value
        else:
            self.field_values[key] = value
        self.approved.setChecked(False)

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
        self.save_preferences()
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
            value = self.current_experiment_id if field.name == 'experiment_id' else self.field_values.get(
                field.name, '10' if field.name == 'af_timeout_s' else '')
            entry = QLineEdit(value)
            entry.textChanged.connect(lambda value, key=field.name: self._edit(key, value))
            if field.optional:
                entry.setPlaceholderText("可选")
            self.values[field.name] = entry
            self.form.addRow(field.label, entry)

    def run(self):
        workflow = self.app.workflow
        if workflow is None:
            self.result_info.setText('尚未连接设备。请到“设备概览”连接现场设备或载入演示，再执行。');return
        from ..action_feedback import defer_read
        token=lambda:(self.op.key if self.op else None,tuple((k,e.text()) for k,e in self.values.items()),self.approved.isChecked(),self.timeout.value())
        if defer_read(self.app,self,self.run,token,self.result_info.setText):return
        if self.session.library is None or self.session.library.device is not workflow.device:self.bind_current()
        if self.session.library is None:
            self.result_info.setText('当前设备不支持本测试入口；请连接 Gateway 或选择采图演示。');return
        if self.op is None:
            self.result_info.setText("请先在左侧选择一个具体操作。")
            return
        if self.op.xy_required and not workflow.xy_enabled:
            self.result_info.setText("此操作需要电动 XY；当前为无 XY 连接。")
            return
        self.save_preferences()
        timeout = float(self.timeout.value())
        params = {name: entry.text() for name, entry in self.values.items()}
        confirmed = self.approved.isChecked()
        if self.op.writes and not self.op.is_stop and not confirmed:
            self.result_info.setText('此操作将写入参数或移动设备，请先勾选本行的操作确认。')
            self.approved.setFocus();return
        self.approved.setChecked(False)
        self.result_info.setText(f"正在执行 {self.op.title}；同步 RPC 可能持续至超时。")
        self.run_button.setEnabled(False)
        try:
            workflow.run_hardware_test(self.session, self.op.key, params,
                                       confirmed=confirmed, timeout_s=timeout)
        except Exception as exc:  # noqa: BLE001 — surface as result text
            self.run_button.setEnabled(True)
            self.result_info.setText("未能提交单步：" + str(exc))

    def start_batch(self):
        w=self.app.workflow
        if w is None:self.result_info.setText('请先在“设备概览”连接或载入演示，再选择测试项目。');return
        from ..action_feedback import defer_read
        if defer_read(self.app,self,self.start_batch,lambda:id(self.app.workflow),self.result_info.setText):return
        if self.session.library is None or self.session.library.device is not w.device:self.bind_current()
        if self.session.library is None:return
        from .hardware_batch import BatchDialog
        dialog=BatchDialog(self)
        from PySide6.QtWidgets import QDialog
        if dialog.exec()!=QDialog.Accepted:return
        mode='motorized' if dialog.xy.isChecked() else 'no_xy'
        w.device.xy_enabled=dialog.xy.isChecked()
        self.app.xy_mode=self.app._connected_mode=mode
        self.app.preferences.update(xy_mode=mode)
        self.app.xy_chip.setText('有电动 XY' if dialog.xy.isChecked() else '无电动 XY · 仅 Z')
        self.app._live_xy=None
        w.run_hardware_batch(self.session,dialog.plan,timeout_s=self.timeout.value(),zero_z=dialog.zero_z.isChecked(),zero_xy=dialog.xy.isChecked() and dialog.zero_xy.isChecked())

    def export_batch(self):
        if not self.batch_report:self.result_info.setText('尚无一键测试报告，请先运行“一键测试”。');return
        name,_=QFileDialog.getSaveFileName(self,'导出一键测试报告','hardware-test-report.md','Markdown (*.md);;JSON (*.json)')
        if not name:return
        target=Path(name)
        if target.suffix.lower()=='.json':target.write_text(json.dumps(self.batch_report,ensure_ascii=False,indent=2),encoding='utf-8')
        else:target.write_text((Path(self.batch_report['directory'])/'report.md').read_text(encoding='utf-8'),encoding='utf-8')

    def request_stop(self):
        busy=bool(self.app.workflow and self.app.workflow.busy)
        self.app.stop()
        if busy:
            self.session.note_stop_request()
            self.result_info.setText('已请求停止后续测试；当前设备调用可能仍需等待返回或超时。')
        else:self.result_info.setText('当前没有测试正在运行，待执行请求已取消。')

    def on_workflow_event(self, kind, data, workflow):
        if kind in ('hardware_batch_progress','hardware_batch_result'):
            report=data[0];self.batch_report=report;self.latest=report
            self.current_experiment_id=getattr(workflow.device,'_experiment_id',None) or ''
            counts=report.get('counts',{})
            self.result_info.setText(('测试中' if report['state']=='running' else '测试结束')+' · '+str(counts)+' · '+report['directory'])
            self.log_view.setPlainText('\n\n'.join('['+r.get('result','')+'] '+r['title']+'\n'+r.get('message','')+'\n建议：'+r.get('advice','—') for r in report['items']))
            self.result_pill.set_kind('warn' if any(r.get('result')=='不OK' for r in report['items']) else 'ok',report['state'])
            return
        if kind != "hardware_result":
            return
        report = data[0]
        self.latest = report
        self.run_button.setEnabled(True)
        status = report.get("status", "?")
        self.result_pill.set_kind("ok" if status == "success" else "bad",
                                  status + (" · 状态未核验" if report.get("recovery_required") else ""))
        self.result_info.setText(
            f"{report.get('title', report.get('operation'))} · {status}"
            + ("；请检查设备当前状态" if report.get("recovery_required") else "；详见日志"))
        self.log_view.appendPlainText(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if report['operation'] in ('connect', 'disconnect'):
            self.current_experiment_id = ''
            if 'experiment_id' in self.values:
                self.values['experiment_id'].setText('')
        result = report.get("result") or {}
        if status == "success" and report["operation"] in ("load_experiment", "clone_experiment") \
                and result.get('experiment_id'):
            self.current_experiment_id = result['experiment_id']
            self._experiment_device = workflow.device
            if 'experiment_id' in self.values:
                self.values['experiment_id'].setText(self.current_experiment_id)
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
        can_run = can_run and self.session.library.device is self.app.workflow.device
        self.batch_button.setEnabled(True)
        self.batch_report_button.setEnabled(True)
        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(True)
