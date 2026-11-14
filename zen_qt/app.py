"""Main window: top status bar, workspace navigation, log dock, recovery lock.

Device state lives exclusively in `self.workflow` (zen_workflow.Workflow);
this shell only drains its event queue and renders what it reports — the same
separation the Tk workbench had.
"""

import json
import os
from pathlib import Path
from queue import Empty
from threading import Thread

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QStackedWidget, QToolButton, QVBoxLayout, QWidget, QButtonGroup,
)

from zen_hardware.gateway import GatewayDevice, secure_channel
from zen_hardware.local_config import gateway_settings
from zen_hardware.mark_simulator import SimulatedMarkDevice
from zen_hardware.simulator import SimulatedDevice
from zen_fit import fit_demo, fit_field, FIELD_CONVENTION, validate_recipe
from zen_preferences import Preferences
from zen_records import confirm_recovery, pending_recovery, write_json, utc_now
from zen_workflow import Recipe, Workflow
from . import theme, worker
from .dialogs import GatewayDialog, RecoveryDialog
from .widgets import Card, Chip, LogDock, hbox, label, vbox

DEMO_POINTS = ((0, 0), (500e-6, 0), (500e-6, 500e-6), (0, 500e-6))
DEMO_CONVENTION = "演示约定：Rx=atan(dZ/dY)，Ry=-atan(dZ/dX)，角度单位 µrad"

READY_STATES = {"就绪", "完成", "已中止"}
RECOVERY_STATE = "设备状态待确认"


class WorkbenchWindow(QMainWindow):
    def __init__(self, data_root):
        super().__init__()
        self.data_root = Path(data_root)
        self.preferences = Preferences(self.data_root / "preferences" / "ui.json")
        self.gateway_overrides = dict(self.preferences.data.get("connection", {}))
        self.session = dict(self.preferences.data.get("session", {}))
        self.xy_mode = self.preferences.data.get("xy_mode", "motorized")
        self._connected_mode = self.xy_mode
        self.workflow = None
        self.pending = None
        self.field_recipe = None
        self.field_verified = False
        self.verified_xy = None
        self.current_xy = None
        self.hw_session = None
        self.recovery_runs = pending_recovery(self.data_root, simulated=True)
        self.field_recovery_runs = pending_recovery(self.data_root, simulated=False)
        self._live_z = None
        self._live_xy = None

        self.setWindowTitle("ZEN 表面测角工作台 v2.5.1")
        self.resize(1600, 980)
        self.setMinimumSize(1000, 620)
        self.setStyleSheet(theme.QSS)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = vbox(root, spacing=0, margins=0)

        outer.addWidget(self._build_topbar())
        self.lockbar = self._build_lockbar()
        outer.addWidget(self.lockbar)
        self.lockbar.hide()

        middle = QWidget()
        mid_lay = hbox(middle, spacing=0, margins=0)
        mid_lay.addWidget(self._build_nav())
        self.pages = {}
        self.stack = QStackedWidget()
        mid_lay.addWidget(self.stack, 1)
        outer.addWidget(middle, 1)

        self.dock = LogDock(self)
        outer.addWidget(self.dock)

        self._build_pages()

        shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        shortcut.setContext(Qt.ApplicationShortcut)
        shortcut.activated.connect(self.stop)

        if self.xy_mode == "no_xy":
            self.log("info", "无 XY 模式：仅单点采图；角度扫描与水平校验不可用。")
        if self.preferences.warning:
            self.log("warn", self.preferences.warning)
        if self.recovery_runs or self.field_recovery_runs:
            self.log("lock", "检测到未确认的设备状态，写入与运动已锁定。")

        worker.bridge(self).done.connect(self._rpc_done)
        worker.bridge(self).fail.connect(self._rpc_fail)
        worker.bridge(self).progress.connect(self._rpc_progress)

        self.pump = worker.EventPump(self, self._pump, 35)
        self.z_timer = QTimer(self)
        self.z_timer.setInterval(1000)
        self.z_timer.timeout.connect(self._poll_position)
        self.z_timer.start()
        self.refresh_ui()

    # ---------- shell construction ----------

    def _build_topbar(self):
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(56)
        lay = hbox(bar, spacing=10, margins=(16, 0, 16, 0))
        brand = QLabel("■ ZEN 表面测角工作台")
        brand.setObjectName("brand")
        brand.setStyleSheet(f"color:{theme.ACCENT}")
        lay.addWidget(brand)
        lay.addWidget(label("v2.5.1", role="muted"))
        self.conn_chip = Chip("未连接")
        self.mode_chip = Chip("真实设备")
        self.mode_chip.set_kind("real")
        self.xy_chip = Chip("有电动 XY" if self.xy_mode == "motorized" else "无电动 XY · 单点")
        self.recovery_chip = Chip("无待恢复项")
        for chip in (self.conn_chip, self.mode_chip, self.xy_chip, self.recovery_chip):
            lay.addWidget(chip)
        readouts = QFrame()
        readouts.setStyleSheet(f"border:1px solid {theme.LINE}; border-radius:8px; background:#fbfcfd;")
        rl = hbox(readouts, spacing=0, margins=0)
        self.read_labels = {}
        for index, (key, unit) in enumerate((("X", "µm"), ("Y", "µm"), ("Z", "µm"), ("物镜", ""))):
            cell = QWidget()
            cl = vbox(cell, spacing=0, margins=(10, 3, 10, 3))
            head = label(f"{key} {unit}".rstrip(), role="muted")
            head.setStyleSheet("font-size:10px")
            cl.addWidget(head)
            value = label("—", mono=True)
            value.setStyleSheet("font-size:13px; font-weight:600")
            self.read_labels[key] = value
            cl.addWidget(value)
            rl.addWidget(cell)
            if index < 3:
                sep = QFrame()
                sep.setStyleSheet(f"background:{theme.LINE}; max-width:1px;")
                rl.addWidget(sep)
        lay.addWidget(readouts)
        lay.addStretch(1)
        self.stop_button = QPushButton("■ 停止")
        self.stop_button.setObjectName("danger")
        self.stop_button.setShortcut("Esc")
        self.stop_button.clicked.connect(self.stop)
        lay.addWidget(self.stop_button)
        return bar

    def _build_lockbar(self):
        bar = QFrame()
        bar.setObjectName("lockbar")
        lay = hbox(bar, spacing=12, margins=(16, 8, 16, 8))
        self.lock_title = label("⚠ 设备状态待确认 · 写入与运动已锁定")
        self.lock_title.setStyleSheet(f"color:{theme.BAD}; font-size:14px; font-weight:700;")
        lay.addWidget(self.lock_title)
        self.lock_text = label("", wrap=True)
        self.lock_text.setStyleSheet("color:#6d2a22; font-size:12px;")
        lay.addWidget(self.lock_text, 1)
        view = QPushButton("查看运行记录", clicked=lambda: self.navigate("records"))
        unlock = QPushButton("逐项确认并解锁…", clicked=self.open_recovery)
        unlock.setStyleSheet(f"border-color:{theme.BAD}; color:{theme.BAD}; font-weight:600;")
        lay.addWidget(view)
        lay.addWidget(unlock)
        return bar

    def _build_nav(self):
        nav = QFrame()
        nav.setObjectName("navbar")
        nav.setFixedWidth(196)
        lay = vbox(nav, spacing=3, margins=(10, 12, 10, 12))
        lay.addWidget(label("工作区", role="muted"))
        self.nav_buttons = {}
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        entries = (
            ("device", "1  设备与自检"), ("tilt", "2  表面测角"),
            ("marks", "3  对位采图"), ("records", "4  记录与分析"),
        )
        for key, text in entries:
            button = self._nav_button(key, text)
            lay.addWidget(button)
            self.nav_buttons[key] = button
        lay.addWidget(label("工程", role="muted"))
        for key, text in (("hardware", "⚙  硬件单步调试"), ("settings", "≡  设置")):
            button = self._nav_button(key, text)
            lay.addWidget(button)
            self.nav_buttons[key] = button
        lay.addStretch(1)
        session_card = Card()
        session_lay = session_card._lay
        session_lay.setSpacing(4)
        session_lay.addWidget(label("样品 / 批次", role="muted"))
        self.sample_edit = QLineEdit(str(self.session.get("sample", "")))
        self.sample_edit.setPlaceholderText("如 W-0412 · Lot A7")
        self.operator_edit = QLineEdit(str(self.session.get("operator", "")))
        self.operator_edit.setPlaceholderText("操作员")
        session_lay.addWidget(self.sample_edit)
        session_lay.addWidget(self.operator_edit)
        session_lay.addWidget(label("本次会话目录", role="muted"))
        path = label(str(self.data_root), role="muted")
        path.setStyleSheet("font-size:10px")
        path.setWordWrap(True)
        session_lay.addWidget(path)
        lay.addWidget(session_card)
        for edit in (self.sample_edit, self.operator_edit):
            edit.editingFinished.connect(self._save_session)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setFixedWidth(212)
        scroll.setWidget(nav)
        return scroll

    def _nav_button(self, key, text):
        button = QToolButton()
        button.setObjectName("nav")
        button.setText(text)
        button.setCheckable(True)
        button.clicked.connect(lambda _=False, k=key: self.navigate(k))
        self.nav_group.addButton(button)
        return button

    def _build_pages(self):
        from .pages.device import DevicePage
        from .pages.tilt import TiltPage
        from .pages.marks import MarksPage
        from .pages.records import RecordsPage
        from .pages.hardware import HardwarePage
        from .pages.settings import SettingsPage
        self.page_containers = {}
        for key, cls in (("device", DevicePage), ("tilt", TiltPage),
                         ("marks", MarksPage), ("records", RecordsPage),
                         ("hardware", HardwarePage), ("settings", SettingsPage)):
            page = cls(self)
            self.pages[key] = page
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setWidget(page)
            self.page_containers[key] = scroll
            self.stack.addWidget(scroll)
        self.navigate("device")

    # ---------- navigation / logging ----------

    def navigate(self, key):
        self.stack.setCurrentWidget(self.page_containers[key])
        self.nav_buttons[key].setChecked(True)
        page = self.pages[key]
        if hasattr(page, "on_show"):
            page.on_show()

    def log(self, level, text):
        self.dock.add(level, text)

    def rpc_trace_text(self):
        device = getattr(self.workflow, "device", None) if self.workflow else None
        trace = device.rpc_trace() if device is not None and hasattr(device, "rpc_trace") else []
        if not trace:
            return "尚无 RPC 调用记录；连接或单步执行后自动生成。"
        return "\n".join(f"{row.get('at', '')}  {row.get('method', '')}  →  {row.get('code', '')}"
                         for row in trace)

    def diagnostic_text(self):
        report = getattr(self.workflow, "mark_diagnostic_report", None) if self.workflow else None
        return json.dumps(report, ensure_ascii=False, indent=2) if report else "尚无诊断报告。"

    def export_diagnostic_bundle(self):
        from PySide6.QtWidgets import QFileDialog
        bundle = {"kind": "diagnostic_bundle", "exported_at": utc_now(),
                  "software": "ZEN 表面测角工作台 v2.5.1 (Qt)",
                  "simulated": bool(self.workflow and self.workflow.simulated),
                  "xy_mode": self.xy_mode,
                  "rpc_trace": (self.workflow.device.rpc_trace()
                                if self.workflow and hasattr(self.workflow.device, "rpc_trace") else []),
                  "mark_diagnostic": getattr(self.workflow, "mark_diagnostic_report", None)}
        path, _ = QFileDialog.getSaveFileName(self, "导出诊断包", "zen-diagnostic.json",
                                              "JSON (*.json)")
        if not path:
            return
        try:
            write_json(Path(path), bundle)
        except OSError:
            self.log("error", "诊断包写入失败，请检查目标目录权限。")
        else:
            self.log("ok", f"诊断包已导出：{path}（不含令牌、请求体和实验 XML）")

    # ---------- device lifecycle (mirrors Tk Workbench) ----------

    def _replace_workflow(self, device, *, simulated, validate, fit):
        old = self.workflow
        if old and old.busy:
            raise RuntimeError('已有设备任务，请等待完成后更换连接')
        if old:
            self._drain(old)
            if isinstance(old.device, GatewayDevice):
                old.device.close()
        self.workflow = Workflow(device, self.data_root, simulated=simulated,
                                 validate=validate, fit=fit)
        self.pending = None
        self._connected_mode = self.xy_mode
        self.field_verified = False
        self.verified_xy = None
        self.current_xy = None
        self._live_z = self._live_xy = None
        self.hw_session = None
        self.clear_result()
        for page in self.pages.values():
            handler = getattr(page, 'on_device_changed', None)
            if handler:
                handler(self.workflow)
        self.log('info', '已绑定 ' + ('离线演示设备' if simulated else '现场 Gateway 通道'))

    def _drain(self, workflow):
        try:
            while True:
                kind, *data = workflow.events.get_nowait()
                self._on_event(workflow, kind, data)
        except Empty:
            pass

    def clear_result(self):
        for page in self.pages.values():
            if hasattr(page, "clear_result"):
                page.clear_result()

    def connect_field(self):
        if self.workflow and self.workflow.busy:
            return
        try:
            address, pem_path, token = gateway_settings({**os.environ, **self.gateway_overrides})
        except ValueError as exc:
            self.log("error", str(exc))
            return

        def channel():
            return secure_channel(address, pem_path.read_bytes(), token)

        env = {**os.environ, **self.gateway_overrides}
        flag = lambda key: env.get(key, "").strip().lower() in ("1", "true", "yes")
        self._replace_workflow(
            GatewayDevice(channel, xy_enabled=self.xy_mode != "no_xy",
                          allow_focus_v1=flag("ZEN_ALLOW_FOCUS_V1"),
                          allow_unverified_af=flag("ZEN_ALLOW_UNVERIFIED_AF"),
                          allow_definite_focus=flag("ZEN_ALLOW_DEFINITE_FOCUS")),
            simulated=False, validate=validate_recipe, fit=fit_field)
        self.log("move", "正在连接现场 Gateway（只读验证）…")
        self.workflow.connect()
        self.refresh_ui()

    def connect_demo(self, kind):
        """Offline demo: 'tilt' runs the 4-point demo; 'marks' binds the mark simulator."""
        if self.workflow and self.workflow.busy:
            return
        if kind != 'marks' and self.xy_mode == 'no_xy':
            self.log('warn', '无 XY 模式仅支持单点采图。测角演示请先切换为有电动 XY。')
            return
        if kind == "marks":
            self._replace_workflow(SimulatedMarkDevice(xy_enabled=self.xy_mode != "no_xy"),
                                   simulated=True, validate=validate_recipe, fit=fit_demo)
            self.workflow.connect()
            self.pages["marks"].load_demo_defaults()
            self.navigate("marks")
        else:
            fault = {"af_timeout": "af_timeout"}.get(kind)
            single = kind == "single"
            self._replace_workflow(SimulatedDevice(fault=fault), simulated=True,
                                   validate=validate_recipe, fit=fit_demo)
            recipe = Recipe("演示配方", "Demo AF", DEMO_POINTS, 1e-6, 3, 2, DEMO_CONVENTION)
            self.pages['tilt'].apply_recipe(recipe)
            self.pending = (recipe, single)
            self.workflow.connect()
            self.navigate("tilt")
        self.refresh_ui()

    def disconnect(self):
        if self.workflow is None or self.workflow.busy:
            return
        old, self.workflow = self.workflow, None
        self._drain(old)
        if isinstance(old.device, GatewayDevice):
            old.device.close()
        self._live_z = self._live_xy = None
        self.field_verified = False
        self.verified_xy = None
        self.current_xy = None
        for page in self.pages.values():
            handler = getattr(page, 'on_device_changed', None)
            if handler:
                handler(None)
        self.log("info", "已断开客户端通道；物理动作是否停止需在设备端确认。")
        self.refresh_ui()

    def stop(self):
        if self.workflow:
            self.pending = None
            self.workflow.stop()
            self.log("warn", "已请求停止后续采集；正在等待的同步 RPC 不能取消，请在设备端确认停止。")

    def set_xy_mode(self, mode):
        if mode not in ('motorized', 'no_xy'):
            raise ValueError('未知 XY 模式')
        if mode == self.xy_mode:
            return
        if self.workflow and self.workflow.busy:
            return
        self.xy_mode = mode
        self._connected_mode = mode
        self.preferences.update(xy_mode=mode)
        self.field_verified = False
        self.disconnect()
        self.xy_chip.setText("有电动 XY" if mode == "motorized" else "无电动 XY · 单点")
        self.log("info", "模式已切换，请重新连接。" + ("无 XY 模式不能角度扫描，也不能校验水平漂移。" if mode == "no_xy" else ""))
        self.refresh_ui()

    # ---------- recovery / lock ----------

    def pending_runs(self):
        return tuple(self.recovery_runs) + tuple(self.field_recovery_runs)

    def locked(self):
        return bool(self.pending_runs()) or (
            self.workflow is not None and self.workflow.state == RECOVERY_STATE)

    def device_ready(self):
        return self.workflow is not None and self.workflow.state in READY_STATES and not self.locked()

    def open_recovery(self):
        if self.workflow and self.workflow.busy:
            return
        dialog = RecoveryDialog(self)
        dialog.exec()

    def confirm_recovery(self):
        if self.workflow and self.workflow.busy:
            return False
        from PySide6.QtWidgets import QMessageBox
        try:
            if self.workflow and self.workflow.simulated:
                self.workflow.device.confirm_simulated_recovery()
            for run in self.pending_runs():
                confirm_recovery(run, xy_confirmed=True, af_z_confirmed=True)
        except OSError:
            QMessageBox.critical(self, "恢复记录失败", "恢复记录保存失败，请检查数据目录。")
            return False
        self.recovery_runs = pending_recovery(self.data_root, simulated=True)
        self.field_recovery_runs = pending_recovery(self.data_root, simulated=False)
        self.field_verified = False
        if self.workflow:
            self.workflow._state('通信就绪（硬件单步）', detail='恢复确认已记录；业务采集前请重新连接并校验')
        self.pages['marks'].invalidate_confirmation()
        self.log("ok", "恢复确认已记录；请重新连接并校验。")
        self.refresh_ui()
        return not self.locked()

    # ---------- workflow event handling ----------

    def _pump(self):
        workflow = self.workflow
        if workflow is not None:
            self._drain(workflow)
            if self.pending and not workflow.busy:
                recipe, single_point = self.pending
                self.pending = None
                if workflow.state == "就绪":
                    try:
                        workflow.start(recipe, path_confirmed=True, single_point=single_point)
                    except (RuntimeError, ValueError) as exc:
                        self.log("error", str(exc))
        self.refresh_ui()

    def _on_event(self, workflow, kind, data):
        if kind == 'read_only_result':
            self._rpc_done(data[0], data[1])
            return
        if kind == 'read_only_error':
            self._rpc_fail(data[0], data[1])
            return
        if kind == "state":
            state, point, detail = data
            message = f"{state} · 点位 {point or '—'} {detail}".rstrip()
            level = "error" if state == RECOVERY_STATE else ("ok" if state in READY_STATES else "info")
            self.log(level, ("离线演示 · " if workflow.simulated else "") + message)
            if state == RECOVERY_STATE:
                if workflow.simulated:
                    self.recovery_runs = pending_recovery(self.data_root, simulated=True)
                else:
                    self.field_verified = False
                    self.field_recovery_runs = pending_recovery(self.data_root, simulated=False)
        elif kind == "xy":
            self.current_xy = tuple(data[0])
            self._live_xy = self.current_xy
        elif kind == "z_only":
            self._live_z = data[0]
        elif kind == "verified_xy":
            self.verified_xy = tuple(data[0])
            self.field_verified = True
            self.log("ok", f"单点 AF 校验完成：{self.verified_xy[0]*1e6:.3f}, {self.verified_xy[1]*1e6:.3f} µm")
        elif kind == "taught":
            self.log("ok", f"示教 XY：{data[0][0]*1e6:.3f}, {data[0][1]*1e6:.3f} µm")
        elif kind == "row":
            row = data[0]
            state = {"ok": "完成", "failed": "失败", "stopped": "中止"}[row.status]
            self.log("ok" if row.status == "ok" else "error",
                     f"点 {row.index} {state}" + (f" · AF Z = {row.z*1e6:.3f} µm" if row.z is not None else ""))
        elif kind == "result":
            result, run_path = data
            self._stamp_session(run_path)
            if result:
                self.log("ok", f"拟合完成：Rx {result['rx_urad']:+.3f} µrad · Ry {result['ry_urad']:+.3f} µrad "
                               f"· RMS {result['rms_m']*1e6:.3f} µm · PV {result['pv_m']*1e6:.3f} µm → {run_path}")
        elif kind == "stop":
            self.log("warn", data[0])
        elif kind == "mark_diagnostic":
            report = data[0]
            self._stamp_session(getattr(workflow, "mark_run_path", None))
            self.log("error" if report.get("kind") == "capture_failure" else "info",
                     f"诊断报告已生成（{report.get('kind', '')}）；详见「诊断」页签")
        elif kind == "mark_z":
            layer, z, xy = data
            self.log("info", f"已读取 {'上' if layer == 'upper' else '下'} mark Z = {z*1e6:.6f} µm")
        elif kind == "mark_error":
            self.log("error", data[0])
        elif kind == "hardware_result":
            report = data[0]
            self.log("error" if report.get("status") != "success" else "ok",
                     f"硬件单步 {report.get('title', report.get('operation'))} → {report.get('status')}")
        for page in self.pages.values():
            handler = getattr(page, "on_workflow_event", None)
            if handler:
                handler(kind, data, workflow)

    def _stamp_session(self, run_path):
        if run_path is None:
            return
        info = {"sample": self.sample_edit.text().strip(),
                "operator": self.operator_edit.text().strip(),
                "recorded_at": utc_now()}
        if not (info["sample"] or info["operator"]):
            return
        try:
            write_json(Path(run_path) / "session.json", info)
        except OSError:
            pass

    def _save_session(self):
        self.session = {"sample": self.sample_edit.text().strip(),
                        "operator": self.operator_edit.text().strip()}
        self.preferences.update(session=self.session)

    # ---------- live position readout ----------

    def _poll_position(self):
        workflow = self.workflow
        if workflow is None or workflow.busy or workflow.state in ("未连接", "连接检查"):
            return
        def read(device):
            out = {}
            if hasattr(device, 'read_z'):
                out['z'] = device.read_z()
            if workflow.xy_enabled:
                out['xy'] = device.read_xy()
            return out
        try:
            workflow.read_only('position', read)
        except RuntimeError:
            pass

    def _rpc_done(self, tag, value):
        if tag == "position":
            self._z_inflight = False
            if value.get("z") is not None:
                self._live_z = value["z"]
            if value.get("xy") is not None:
                self._live_xy = tuple(value["xy"])
        for page in self.pages.values():
            handler = getattr(page, "on_rpc_result", None)
            if handler:
                handler(tag, value)

    def _rpc_fail(self, tag, error):
        if tag == 'position':
            self._z_inflight = False
            self._live_z = self._live_xy = None
        for page in self.pages.values():
            handler = getattr(page, "on_rpc_error", None)
            if handler:
                handler(tag, error)

    def _rpc_progress(self, tag, payload):
        for page in self.pages.values():
            handler = getattr(page, "on_rpc_progress", None)
            if handler:
                handler(tag, payload)

    # ---------- UI refresh ----------

    def refresh_ui(self):
        workflow = self.workflow
        busy = bool(workflow and workflow.busy)
        locked = self.locked()
        if workflow is None:
            self.conn_chip.setText("未连接")
            self.conn_chip.set_kind(None)
        else:
            self.conn_chip.setText(("离线演示" if workflow.simulated else "Gateway") + f" · {workflow.state}")
            self.conn_chip.set_kind("bad" if workflow.state == RECOVERY_STATE else "ok" if workflow.state in READY_STATES else "warn")
        self.mode_chip.setText("离线演示" if (workflow and workflow.simulated) else "真实设备")
        self.mode_chip.set_kind("warn" if (workflow and workflow.simulated) else "real")
        runs = self.pending_runs()
        if runs or (workflow and workflow.state == RECOVERY_STATE):
            self.recovery_chip.setText("⚠ 待恢复 · 已锁定" if runs else "⚠ 状态待确认")
            self.recovery_chip.set_kind("bad")
        else:
            self.recovery_chip.setText("无待恢复项")
            self.recovery_chip.set_kind("ok")
        self.read_labels["X"].setText("—" if self._live_xy is None else f"{self._live_xy[0]*1e6:.2f}")
        self.read_labels["Y"].setText("—" if self._live_xy is None else f"{self._live_xy[1]*1e6:.2f}")
        self.read_labels["Z"].setText("—" if self._live_z is None else f"{self._live_z*1e6:.2f}")
        self.stop_button.setEnabled(busy)
        if locked:
            self.lock_text.setText(
                "存在未确认的设备状态；请在 ZEN 现场确认 Z、AF 与采集均已停止。只读查询和「停止」仍可用。")
            self.lockbar.show()
        else:
            self.lockbar.hide()
        for page in self.pages.values():
            handler = getattr(page, "refresh_enabled", None)
            if handler:
                handler()

    def closeEvent(self, event):
        if self.workflow and self.workflow.busy:
            self.stop()
            QMessageBox.information(self, "任务仍在处理",
                                    "已请求停止后续采集。请等待本轮记录保存后关闭。")
            event.ignore()
            return
        self._save_session()
        self.pages['marks'].persist()
        self.pages['hardware'].save_preferences()
        if self.workflow and isinstance(self.workflow.device, GatewayDevice):
            self.workflow.device.close()
        self.pump.timer.stop()
        self.z_timer.stop()
        event.accept()
