"""设备与自检 page: connection, gateway preflight, service discovery."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton, QWidget

from zen_hardware import preflight
from .. import worker
from ..dialogs import GatewayDialog
from ..widgets import Card, CheckItem, Checklist, Pill, StepStrip, hbox, label, note, vbox


def _page(widget, title, subtitle):
    """Standard page chrome: title row + step strip area."""
    lay = vbox(widget, spacing=12, margins=(20, 16, 20, 12))
    head_row = QWidget()
    head = hbox(head_row, spacing=12, margins=0)
    col = QWidget()
    cl = vbox(col, spacing=2, margins=0)
    t = label(title, role="title")
    t.setStyleSheet("font-size:16px")
    cl.addWidget(t)
    cl.addWidget(label(subtitle, role="muted"))
    head.addWidget(col)
    head.addSpacing(6)
    head.addStretch(1)
    lay.addWidget(head_row)
    return lay, head


class DevicePage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self._last_preflight_data = None
        self._auto_preflight_done = False
        lay, head = _page(self, "设备与自检",
                          "连接 Gateway → 自动预检 → 可选的服务发现与硬件单步调试")
        self.steps = StepStrip(["连接", "预检", "服务发现", "就绪"])
        head.addWidget(self.steps)

        body = QWidget()
        bl = hbox(body, spacing=11, margins=0)
        lay.addWidget(body, 1)

        # ---- left column ----
        left = QWidget()
        ll = vbox(left, spacing=11, margins=0)
        bl.addWidget(left, 3)

        conn_card = Card("连接")
        info_row = QWidget()
        il = hbox(info_row, spacing=24, margins=(0, 0, 0, 6))
        self.conn_labels = {}
        for key, name in (("state", "状态"), ("mode", "模式"), ("xy", "XY"), ("services", "服务")):
            cell = QWidget()
            cl2 = vbox(cell, spacing=1, margins=0)
            cl2.addWidget(label(name, role="muted"))
            value = label("—")
            value.setStyleSheet("font-weight:600")
            cl2.addWidget(value)
            self.conn_labels[key] = value
            il.addWidget(cell)
        il.addStretch(1)
        conn_card.add(info_row)
        btn_row = QWidget()
        rl = hbox(btn_row, spacing=8, margins=0)
        self.connect_button = QPushButton("连接真实设备…", clicked=self._connect_real)
        self.connect_button.setObjectName("primary")
        rl.addWidget(self.connect_button)
        self.reconnect_button = QPushButton("按已存配置重连", clicked=self.app.connect_field)
        rl.addWidget(self.reconnect_button)
        rl.addWidget(QPushButton("离线演示（测角）", clicked=lambda: self.app.connect_demo("normal")))
        rl.addWidget(QPushButton("离线演示（采图）", clicked=lambda: self.app.connect_demo("marks")))
        self.disconnect_button = QPushButton("断开", clicked=self.app.disconnect)
        rl.addWidget(self.disconnect_button)
        rl.addStretch(1)
        conn_card.add(btn_row)
        ll.addWidget(conn_card)

        self.preflight_pill = Pill("未运行")
        preflight_card = Card("预检", trailing=self.preflight_pill)
        self.preflight_summary = label("连接真实设备后自动运行只读预检。", role="muted", wrap=True)
        preflight_card.add(self.preflight_summary)
        row = QWidget()
        rl = hbox(row, spacing=8, margins=0)
        self.preflight_button = QPushButton("重新预检", clicked=self._do_preflight)
        rl.addWidget(self.preflight_button)
        rl.addStretch(1)
        preflight_card.add(row)
        self.preflight_detail = label("", role="muted", wrap=True, mono=True)
        preflight_card.add(self.preflight_detail)
        ll.addWidget(preflight_card)

        self.scan_pill = Pill("—")
        services_card = Card("服务发现", trailing=self.scan_pill)
        services_card.add(label(
            "通过 gRPC reflection 枚举本机 Gateway 注册的服务；只读，不影响设备状态。",
            role="muted", wrap=True))
        row = QWidget()
        rl = hbox(row, spacing=8, margins=0)
        self.scan_button = QPushButton("扫描 ZEN 服务", clicked=lambda: self._scan_services(False))
        rl.addWidget(self.scan_button)
        self.scan_all = QPushButton("列出全部服务", clicked=lambda: self._scan_services(True))
        rl.addWidget(self.scan_all)
        rl.addStretch(1)
        services_card.add(row)
        self.services_list = label("—", role="muted", wrap=True, mono=True)
        self.services_list.setStyleSheet("font-size:11px")
        services_card.add(self.services_list)
        ll.addWidget(services_card)

        # ---- right column ----
        right = QWidget()
        rl2 = vbox(right, spacing=11, margins=0)
        right.setFixedWidth(320)
        bl.addWidget(right)
        check_card = Card("就绪检查", trailing=label("自动", role="muted"))
        self.checklist = Checklist()
        self.check_items = {
            "connected": CheckItem("设备已连接"),
            "xy": CheckItem("XY 模式已确认", "motorized / no-XY"),
            "recovery": CheckItem("无待恢复状态"),
            "recipe": CheckItem("测角配方已加载", "表面测角页 · 可选"),
        }
        self.checklist.set_items(list(self.check_items.values()))
        check_card.add(self.checklist)
        rl2.addWidget(check_card)

        device_card = Card("设备信息")
        self.device_info = label("—", wrap=True, mono=True)
        self.device_info.setStyleSheet("font-size:11.5px")
        device_card.add(self.device_info)
        rl2.addWidget(device_card)

        tips = Card("提示")
        tips.add(note("无电动 XY 时请在连接对话框选「无 XY」。此模式下表面测角不可用，"
                      "对位采图仅作单点拍摄。", kind="info"))
        rl2.addWidget(tips)
        rl2.addStretch(1)
        self.refresh_enabled()

    # ---- checks ----

    def _update_checks(self):
        workflow = self.app.workflow
        connected = bool(workflow and workflow.device is not None)
        self.check_items["connected"].set_state(
            "ok" if connected else "todo", "已连接" if connected else "未连接")
        self.check_items["xy"].set_state(
            "ok" if connected else "todo",
            "电动 XY" if workflow and workflow.xy_enabled
            else ("无 XY" if connected else "motorized / no-XY"))
        self.check_items["recovery"].set_state(
            "bad" if self.app.locked() else ("ok" if connected else "todo"))
        recipe = self.app.field_recipe
        self.check_items["recipe"].set_state(
            "ok" if recipe else "todo",
            f"{recipe.name} · {len(recipe.targets)} 点" if recipe else "未加载（可选）")
        if workflow is None or workflow.device is None:
            states = ["current", "todo", "todo", "todo"]
        elif self._last_preflight_data is None and not workflow.simulated:
            states = ["done", "current", "todo", "todo"]
        else:
            states = ["done", "done", "done", "done" if not self.app.locked() else "bad"]
        self.steps.set_states(states)

    # ---- actions ----

    def _connect_real(self):
        # GatewayDialog applies overrides then calls app.connect_field() itself.
        GatewayDialog(self.app, self).exec()

    def _do_preflight(self):
        workflow = self.app.workflow
        if workflow is None or workflow.device is None or workflow.busy:
            self.app.log("info", "请先连接设备。")
            return
        device = workflow.device
        self.preflight_pill.set_kind("accent", "运行中…")
        self.preflight_summary.setText("正在执行只读预检…")
        worker.run("preflight", lambda: preflight.check_gateway(device))

    def _scan_services(self, all_services=False):
        workflow = self.app.workflow
        if workflow is None or workflow.device is None or workflow.busy:
            self.app.log("info", "请先连接设备。")
            return
        device = workflow.device
        if getattr(device, "channel", None) is None:
            self.app.log("info", "此设备不支持服务扫描（演示模式无 channel）。")
            return
        self.scan_pill.set_kind("accent", "扫描中…")
        worker.run("service_scan:" + ("all" if all_services else "zen"),
                   lambda: self._do_scan(device, all_services))

    def _do_scan(self, device, all_services):
        from zen_hardware.reflection import list_services
        services = list_services(device.channel)
        if not all_services:
            filtered = [s for s in services if "zen" in s.lower()]
            return filtered or services
        return services

    # ---- app bridge callbacks ----

    def on_rpc_result(self, tag, value):
        if tag == "preflight":
            self._preflight_done(value, None)
        elif tag.startswith("service_scan"):
            self._scan_done(value, None)

    def on_rpc_error(self, tag, error):
        if tag == "preflight":
            self._preflight_done(None, error)
        elif tag.startswith("service_scan"):
            self._scan_done(None, error)

    def _preflight_done(self, result, error):
        if error is not None:
            self._last_preflight_data = None
            self.preflight_pill.set_kind("bad", "失败")
            self.preflight_summary.setText(str(error))
            self.app.log("error", f"预检失败：{error}")
        else:
            self._last_preflight_data = result
            ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
            self.preflight_pill.set_kind("ok" if ok else "warn", "通过" if ok else "部分失败")
            if isinstance(result, dict):
                checks = result.get("checks") or result
                lines = []
                for name, value in checks.items():
                    if name == "ok":
                        continue
                    if isinstance(value, dict):
                        state = "✓" if value.get("ok") else "✗"
                        lines.append(f"{state} {name}: {value.get('detail', '')}")
                    else:
                        lines.append(f"· {name}: {value}")
                self.preflight_summary.setText("通过" if ok else "存在失败项，见明细。")
                self.preflight_detail.setText("\n".join(lines[:20]))
            else:
                self.preflight_summary.setText(str(result))
            self.app.log("ok" if ok else "warn", f"预检完成：{'通过' if ok else '部分失败'}")
        self._update_checks()

    def _scan_done(self, result, error):
        if error is not None:
            self.scan_pill.set_kind("bad", "失败")
            self.services_list.setText(str(error))
            self.app.log("error", f"服务扫描失败：{error}")
            return
        self.scan_pill.set_kind("ok", f"{len(result)} 个服务")
        self.services_list.setText("\n".join(result) if result else "（无结果）")
        self.conn_labels["services"].setText(str(len(result)))
        self.app.log("info", f"服务发现完成：{len(result)} 个注册服务")

    # ---- workflow events ----

    def on_workflow_event(self, kind, data, workflow):
        if kind != "state":
            return
        state, _point, _detail = data
        self.conn_labels["state"].setText(state)
        device = workflow.device
        info = []
        if device is not None:
            info.append(f"设备类型 {type(device).__name__}")
            info.append("XY：" + ("电动" if workflow.xy_enabled else "无（单点）"))
            exp = getattr(device, "current_experiment", None) or getattr(device, "experiment", None)
            if exp:
                info.append(f"当前实验 {exp}")
        self.device_info.setText("\n".join(info) or "—")
        if workflow.simulated:
            self.preflight_pill.set_kind("mute", "演示模式")
            self.preflight_summary.setText("演示模式跳过真实预检。")
        # auto-preflight once after a real device becomes ready
        if (state in ("就绪", "通信就绪", "完成") and not workflow.simulated
                and not self._auto_preflight_done and self._last_preflight_data is None):
            self._auto_preflight_done = True
            self._do_preflight()
        self._update_checks()

    def refresh_enabled(self):
        workflow = self.app.workflow
        busy = bool(workflow and workflow.busy)
        connected = bool(workflow and workflow.device is not None)
        simulated = bool(workflow and workflow.simulated)
        self.connect_button.setEnabled(not busy)
        self.reconnect_button.setEnabled(not busy)
        self.disconnect_button.setEnabled(connected and not busy)
        self.preflight_button.setEnabled(not busy and connected and not simulated)
        self.scan_button.setEnabled(not busy and connected and not simulated)
        self.scan_all.setEnabled(self.scan_button.isEnabled())
        self.conn_labels["mode"].setText("演示" if simulated else ("真实设备" if connected else "—"))
        self.conn_labels["xy"].setText(
            "电动" if workflow and workflow.xy_enabled else ("无" if connected else "—"))
        if connected:
            self.conn_labels["state"].setText(workflow.state)
        else:
            self.conn_labels["state"].setText("—")
            self.conn_labels["services"].setText("—")
            self._auto_preflight_done = False
        self._update_checks()
