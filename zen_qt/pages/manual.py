"""Explicit manual motion with the same gate, recovery record and motion policy."""

from pathlib import Path
from time import monotonic
from PySide6.QtWidgets import QWidget, QLineEdit, QPushButton, QCheckBox, QFormLayout
from ..widgets import Card, hbox, vbox, label
from zen_records import RunRecords, write_json, pending_recovery
from zen_recipe_compile import number


class ManualPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        lay = hbox(self)
        self.fields = {}
        motion = Card("手动运动")
        form = QFormLayout()
        for key, title in [
            ("x", "目标绝对 X · µm"),
            ("y", "目标绝对 Y · µm"),
            ("z", "目标绝对 Z · µm"),
            ("safe", "转场 Z · µm"),
            ("low", "允许 Z 下限 · µm"),
            ("high", "允许 Z 上限 · µm"),
        ]:
            e = QLineEdit()
            e.setPlaceholderText("现场示教值")
            self.fields[key] = e
            form.addRow(title, e)
            e.textChanged.connect(
                lambda *_: (
                    self.approved.setChecked(False)
                    if hasattr(self, "approved")
                    else None
                )
            )
        motion.add_layout(form)
        self.approved = QCheckBox("已核对运动范围、转场路线与设备状态")
        motion.add(self.approved)
        row = QWidget()
        rl = hbox(row, margins=0)
        self.xy = QPushButton("XY MoveTo", clicked=lambda: self.move("xy"))
        self.z = QPushButton("Z MoveTo", clicked=lambda: self.move("z"))
        rl.addWidget(self.xy)
        rl.addWidget(self.z)
        motion.add(row)
        motion.add(QPushButton("读取当前位置", clicked=self.read))
        motion._lay.addStretch(1)
        lay.addWidget(motion, 2)
        state = Card("位置与操作反馈")
        self.position = label("X：—\nY：—\nZ：—", wrap=True)
        self.position.setStyleSheet("font-size:20px")
        state.add(self.position)
        self.message = label(
            "手动运动不会自动回原点。所有动作独立记录，异常后需确认恢复。", wrap=True
        )
        state.add(self.message)
        state.add(
            label(
                "无电动 XY：只开放 Z；读数中 XY 明确标为不适用。\n生产任务运行时，手动动作禁用。",
                wrap=True,
                role="muted",
            )
        )
        state._lay.addStretch(1)
        lay.addWidget(state, 1)
        self.approved.toggled.connect(self.refresh_enabled)

    def read(self):
        w = self.app.workflow
        if not w:
            return
        try:
            w.read_only(
                "manual_position",
                lambda d: {
                    "xy": d.read_xy() if w.xy_enabled else None,
                    "z": d.read_z(),
                },
            )
        except RuntimeError as e:
            self.message.setText(str(e))

    def on_rpc_result(self, tag, value):
        if tag == "manual_position":
            xy = value["xy"]
            self.position.setText(
                (
                    "X：不适用\nY：不适用"
                    if xy is None
                    else f"X：{xy[0]*1e6:.3f} µm\nY：{xy[1]*1e6:.3f} µm"
                )
                + f"\nZ：{value['z']*1e6:.3f} µm"
            )

    def on_rpc_error(self, tag, error):
        if tag == "manual_position":
            self.message.setText(str(error))

    def move(self, kind):
        w = self.app.workflow
        try:
            if (
                not w
                or w.busy
                or not self.app.device_ready()
                or not self.approved.isChecked()
            ):
                raise ValueError("设备未就绪或尚未确认运动")
            low = number(self.fields["low"].text(), "Z 下限") * 1e-6
            high = number(self.fields["high"].text(), "Z 上限") * 1e-6
            if low >= high:
                raise ValueError("Z 下限必须小于上限")
            target = tuple(
                number(self.fields[k].text(), k) * 1e-6
                for k in (("x", "y") if kind == "xy" else ("z",))
            )
            safe = (
                number(self.fields["safe"].text(), "转场 Z") * 1e-6
                if kind == "xy"
                else target[0]
            )
            if not low <= safe <= high:
                raise ValueError("目标/转场 Z 超出核对范围")
            if kind == "xy" and not w.xy_enabled:
                raise ValueError("当前无电动 XY")
            self.approved.setChecked(False)

            def action():
                records = None
                interlock = None
                issued = False
                try:
                    records = RunRecords(
                        w.root,
                        name="手动运动",
                        experiment="",
                        targets=(),
                        tolerance_m=1e-6,
                        af_timeout=30,
                        arrival_timeout=15,
                        sign_convention="",
                        simulated=w.simulated,
                        confirmed_start=None,
                    )
                    write_json(
                        records.path / "manual.json",
                        {
                            "kind": kind,
                            "target_m": target,
                            "safe_z_m": safe,
                            "range_m": [low, high],
                            "operator": self.app.operator_edit.text(),
                        },
                    )
                    if w.xy_enabled:
                        interlock = w.motion_device(
                            safe_z_m=safe,
                            z_min_m=low,
                            z_max_m=high,
                            z_tolerance_m=0.5e-6,
                            xy_tolerance_m=1e-6,
                            arrival_timeout_s=15,
                            settle_s=0.2,
                        )
                        issued = True
                        if kind == "xy":
                            interlock.move_xy(*target)
                        else:
                            interlock.move_z(target[0])
                    else:
                        from math import isfinite

                        if not low <= w.device.read_z() <= high:
                            raise ValueError("当前 Z 超出允许范围")
                        w._stop.is_set() and (_ for _ in ()).throw(
                            InterruptedError("已停止")
                        )
                        issued = True
                        w.device.move_z(target[0])
                        deadline = monotonic() + 15
                        stable = 0
                        while True:
                            if w._stop.is_set():
                                raise InterruptedError("已请求停止")
                            z = w.device.read_z()
                            stable = (
                                stable + 1
                                if isfinite(z) and abs(z - target[0]) <= 0.5e-6
                                else 0
                            )
                            if stable >= 3:
                                break
                            if monotonic() >= deadline:
                                raise TimeoutError("Z 到位超时")
                            w._stop.wait(0.02)
                    records.finish("complete", recovery_required=False)
                    w._state("就绪", detail="手动运动已到位并保存记录")
                except Exception as e:
                    if records:
                        records.finish("failed", recovery_required=issued, stage=str(e))
                    w._state("设备状态待确认" if issued else "已中止", detail=str(e))
                finally:
                    if records and interlock:
                        write_json(
                            records.path / "command_log.json",
                            {"commands": interlock.command_log_snapshot()},
                        )

            w._launch(action)
            self.message.setText("运动已提交，请观察设备；停止只阻止后续动作。")
        except (ValueError, RuntimeError) as e:
            self.message.setText(str(e))

    def refresh_enabled(self):
        w = self.app.workflow
        ready = bool(w and not w.busy and self.app.device_ready())
        self.xy.setEnabled(ready and w.xy_enabled and self.approved.isChecked())
        self.z.setEnabled(ready and self.approved.isChecked())
