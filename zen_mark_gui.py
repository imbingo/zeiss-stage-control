"""Operator UI for a single pair of upper/lower mark TIFF images."""

import json
import os
from pathlib import Path
import subprocess
import tkinter as tk
from tkinter import filedialog, ttk

from zen_fit import fit_demo, validate_recipe
from zen_hardware.mark_simulator import SimulatedMarkDevice
from zen_marks import MarkLayer, MarkRecipe
from zen_records import write_json


def _wrap(label, pad=4):
    """Keep long hint text wrapped to the widget's actual width."""
    label.bind("<Configure>", lambda event: label.configure(
        wraplength=max(1, event.width - pad)))
    return label


class MarkCaptureDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("对位图像采集 · 当前 XY")
        self.geometry("1050x760")
        self.minsize(950, 650)
        self.transient(app.root)
        self.demo_config = False
        self.closed = False
        self.last_app_status = None
        self._save_job = None
        self._rpc_report = None
        self._parameter_traces = []
        self.approved = tk.BooleanVar(value=False)
        self.loading_approved = tk.BooleanVar(value=False)
        self.reported_zen_version = tk.StringVar(value="3.12")
        self.capture_kind = tk.StringVar(value="共聚焦采集（ZEN 采集实验）")
        self.use_af = tk.BooleanVar(value=True)
        self.xy_note = tk.StringVar()
        self.mode = tk.StringVar(value="设备：未连接")
        self.message = tk.StringVar(value="先连接现场设备，或载入离线演示。现场 Z 不提供默认值。")
        self.output = tk.StringVar(value=str(app.data_root.parent / "ZENMarkImages"))
        self.fields = {}
        self.read_buttons = []
        self.editable = []
        viewport = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=viewport.yview)
        scrollbar.pack(side="right", fill="y")
        viewport.pack(side="left", fill="both", expand=True)
        viewport.configure(yscrollcommand=scrollbar.set)
        shell = ttk.Frame(viewport, padding=16)
        window = viewport.create_window(0, 0, window=shell, anchor="nw")
        shell.bind("<Configure>", lambda event: viewport.configure(scrollregion=viewport.bbox("all")))
        viewport.bind("<Configure>", lambda event: viewport.itemconfigure(window, width=event.width))
        self.bind("<MouseWheel>", lambda event: viewport.yview_scroll(-int(event.delta/120), "units"))
        ttk.Label(shell, text="上下 mark 图像采集", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        _wrap(ttk.Label(shell, textvariable=self.xy_note)).pack(fill="x", anchor="w", pady=6)
        connection = ttk.Frame(shell)
        connection.pack(fill="x", pady=6)
        ttk.Label(connection, textvariable=self.mode, foreground="#315f89").pack(side="left")
        self.connect_button = ttk.Button(connection, text="连接现场 Gateway", command=self.connect_real)
        self.connect_button.pack(side="right")
        self.demo_button = ttk.Button(connection, text="载入离线演示", command=self.load_demo)
        self.demo_button.pack(side="right", padx=8)
        options = ttk.Frame(shell)
        options.pack(fill="x", pady=3)
        self.mode_controls = []
        for label, mode in (("有电动 XY", "motorized"), ("无电动 XY · 单点采图", "no_xy")):
            control = ttk.Radiobutton(options, text=label, variable=app.xy_mode, value=mode, command=app.change_device_mode)
            control.pack(side="left", padx=(0, 12))
            self.mode_controls.append(control)
        self.kind_combo = ttk.Combobox(options, textvariable=self.capture_kind, state="readonly", width=29,
            values=("共聚焦采集（ZEN 采集实验）", "相机拍照（ZEN 定位 / Snap）"))
        self.kind_combo.pack(side="right")
        _wrap(ttk.Label(shell, text="共聚焦请使用已在 ZEN 采集页验证过的单通道、单 Z 层、单时间点、无拼图实验；不会自动改拍相机图。")).pack(fill="x", anchor="w", pady=3)
        self.af_control = ttk.Checkbutton(shell, text="定位后执行局部 AF（初次连机可取消，仅验证示教 Z 和采集保存）", variable=self.use_af)
        self.af_control.pack(anchor="w")
        _wrap(ttk.Label(shell, text="在 ZEN 中手动调清楚对应 mark 后，点击“读取当前 Z”；上下层可使用不同曝光和照明的实验。")).pack(fill="x", anchor="w", pady=6)
        layers = ttk.Frame(shell)
        layers.pack(fill="x", pady=4)
        for column, (key, title) in enumerate((("upper", "上 mark"), ("lower", "下 mark"))):
            panel = ttk.LabelFrame(layers, text=title, padding=10)
            panel.grid(row=0, column=column, sticky="nsew", padx=(0, 10) if column == 0 else (0, 0))
            layers.columnconfigure(column, weight=1)
            self.fields[key] = {}
            for row, (field, label, initial) in enumerate((("experiment", "ZEN 拍摄实验名", ""),
                                                         ("z_m", "示教 Z (µm)", ""),
                                                         ("half_range_m", "局部 AF 半范围 (±µm)", "5"))):
                ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", pady=3)
                variable = tk.StringVar(value=initial)
                self.fields[key][field] = variable
                entry = ttk.Entry(panel, textvariable=variable, width=24)
                entry.grid(row=row, column=1, sticky="ew", padx=8)
                self.editable.append(entry)
            panel.columnconfigure(1, weight=1)
            button = ttk.Button(panel, text="读取当前 Z", command=lambda k=key: self.read_z(k))
            button.grid(row=3, column=1, sticky="e", pady=(5, 0))
            self.read_buttons.append(button)
        common = ttk.LabelFrame(shell, text="采集检查参数", padding=8)
        common.pack(fill="x", pady=8)
        self.common = {}
        self.common_entries = {}
        defaults = (("xy_tolerance_m", "XY 容差 (µm)", "1"), ("z_tolerance_m", "Z 容差 (µm)", "0.5"),
                    ("af_timeout_s", "AF 超时 (s)", "10"), ("arrival_timeout_s", "Z 到位超时 (s)", "5"),
                    ("capture_timeout_s", "拍摄超时 (s)", "60"), ("settle_s", "稳定等待 (s)", "0.2"))
        for index, (key, title, initial) in enumerate(defaults):
            row, column = divmod(index, 3)
            ttk.Label(common, text=title).grid(row=row, column=column*2, sticky="w", padx=(4, 5), pady=4)
            var = self.common[key] = tk.StringVar(value=initial)
            entry = ttk.Entry(common, textvariable=var, width=10)
            entry.grid(row=row, column=column*2+1, sticky="ew", padx=(0, 16))
            self.editable.append(entry)
            self.common_entries[key] = entry
            common.columnconfigure(column*2+1, weight=1)
        output_row = ttk.Frame(shell)
        output_row.pack(fill="x", pady=5)
        ttk.Label(output_row, text="图像输出目录").pack(side="left")
        output_entry = ttk.Entry(output_row, textvariable=self.output)
        output_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.editable.append(output_entry)
        self.browse_button = ttk.Button(output_row, text="选择目录", command=self.browse)
        self.browse_button.pack(side="right")
        self.approval_button = ttk.Checkbutton(shell, variable=self.approved,
            text="已固定样品且不会手动移动水平位置；已核对 Z 路径/AF 范围、物镜/倍率/ROI，以及通道 0 的单幅采集设置。")
        self.approval_button.pack(anchor="w", pady=8)
        self.loading_approval_button = ttk.Checkbutton(shell, variable=self.loading_approved,
            text="已在 ZEN 核对实验加载/运行不会换物镜或恢复设备位置（人工确认，不代表软件锁定转盘）。")
        self.loading_approval_button.pack(anchor="w", pady=3)
        _wrap(ttk.Label(shell, text="读取 Z 不代表转盘已锁定；实验列表检查无法审查全部参数。未排除换镜风险时，仅做只读检查。",
                  foreground="#973c39")).pack(fill="x", anchor="w", pady=3)
        controls = ttk.Frame(shell)
        controls.pack(fill="x", pady=6)
        self.capture_buttons = []
        for title, keys in (("仅拍上 mark", ("upper",)), ("仅拍下 mark", ("lower",)), ("采集上下两张", ("upper", "lower"))):
            button = ttk.Button(controls, text=title, command=lambda selected=keys: self.capture(selected))
            button.pack(side="left", padx=(0, 8))
            self.capture_buttons.append(button)
        self.stop_button = ttk.Button(controls, text="停止后续拍摄", command=app.stop)
        self.stop_button.pack(side="right")
        utilities = ttk.Frame(shell)
        utilities.pack(fill="x", pady=6)
        self.load_button = ttk.Button(utilities, text="加载采图配置", command=self.load_config)
        self.load_button.pack(side="left")
        self.save_button = ttk.Button(utilities, text="保存采图配置", command=self.save_config)
        self.save_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(utilities, text="打开本轮图片目录", command=self.open_output)
        self.open_button.pack(side="left")
        self.recover_button = ttk.Button(utilities, text="恢复确认", command=self.recover)
        self.recover_button.pack(side="right")
        diagnostics = ttk.Frame(shell)
        diagnostics.pack(fill="x", pady=4)
        ttk.Label(diagnostics, text="ZEN 版本（人工记录，不决定兼容性）").pack(side="left")
        self.version_entry = ttk.Entry(diagnostics, textvariable=self.reported_zen_version, width=10)
        self.version_entry.pack(side="left", padx=6)
        self.editable.append(self.version_entry)
        self.inspect_button = ttk.Button(diagnostics, text="只读检查实验名（不加载）", command=self.inspect_experiments)
        self.inspect_button.pack(side="left", padx=8)
        self.diagnostic_save_button = ttk.Button(diagnostics, text="保存诊断报告", command=self.save_diagnostic)
        self.diagnostic_save_button.pack(side="right")
        self.diagnostic_text = tk.Text(shell, height=6, wrap="word", state="disabled")
        self.diagnostic_text.pack(fill="x", pady=3)
        _wrap(ttk.Label(shell, textvariable=self.message, justify="left")).pack(fill="x", anchor="w", pady=8)
        _wrap(ttk.Label(shell, text="完成后停留在最后拍摄层的焦面，不自动返回。停止/超时不等于物理动作已结束；异常时须确认 XY、Z、AF 与拍摄状态。", foreground="#626873")).pack(side="bottom", fill="x", anchor="w", pady=8)
        self.restore_preferences()
        for variable in [self.output, self.capture_kind, self.use_af, self.app.xy_mode, self.reported_zen_version,
                         *self.common.values(), *(v for group in self.fields.values() for v in group.values())]:
            self._parameter_traces.append((variable, variable.trace_add("write", self.parameters_changed)))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.grab_set()
        self._refresh_job = self.after(100, self.refresh)

    def restore_preferences(self):
        saved = self.app.preferences.data.get("mark_capture")
        if not saved:
            return
        for key, group in self.fields.items():
            for name, variable in group.items():
                if name in saved.get(key, {}):variable.set(saved[key][name])
        for key, variable in self.common.items():
            if key in saved.get("common", {}):variable.set(saved["common"][key])
        if "output" in saved:self.output.set(saved["output"])
        if "reported_zen_version" in saved:self.reported_zen_version.set(saved["reported_zen_version"])
        if "capture_kind" in saved:
            self.capture_kind.set("共聚焦采集（ZEN 采集实验）" if saved["capture_kind"] == "confocal" else "相机拍照（ZEN 定位 / Snap）")
        self.use_af.set(saved.get("use_autofocus", True))
        self.demo_config = saved.get("simulated_configuration", True)
        self.approved.set(False)
        self.loading_approved.set(False)
        self.message.set("已自动恢复上次参数；示教 Z 为历史值，尚未核对本次位置。请先做只读检查，重新核对间隙和实验，不会自动连接或动作。")

    def parameters_changed(self, *args):
        self.approved.set(False)
        self.loading_approved.set(False)
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(350, self.persist_preferences)

    def persist_preferences(self):
        self._save_job = None
        saved = {key: {name: var.get() for name, var in group.items()} for key, group in self.fields.items()}
        saved.update({'entry_unit': 'um', "common": {key: var.get() for key, var in self.common.items()},
                      "output": self.output.get(), "capture_kind": "confocal" if self.capture_kind.get().startswith("共聚焦") else "camera_snap",
                      "use_autofocus": self.use_af.get(), "simulated_configuration": self.demo_config,
                      "reported_zen_version": self.reported_zen_version.get()})
        if not self.app.preferences.update(xy_mode=self.app.xy_mode.get(), mark_capture=saved):
            self.message.set("自动保存配置失败或旧配置不可读；本次参数仍可用，可另存配置文件。")

    def inspect_experiments(self):
        try:
            if self.app.workflow is None:
                raise ValueError("请先连接 Gateway；只读检查不会加载实验、移动 Z 或拍摄")
            names = tuple(group["experiment"].get().strip() for group in self.fields.values())
            self.app.workflow.inspect_mark_experiments(names, reported_zen_version=self.reported_zen_version.get().strip())
            self.message.set("正在只读查询实验名与位置，不加载实验、不动作。")
        except (ValueError, RuntimeError) as error:
            self.message.set(str(error))

    def show_diagnostic(self, report):
        self._rpc_report = report
        self.diagnostic_text.configure(state="normal")
        self.diagnostic_text.delete("1.0", "end")
        self.diagnostic_text.insert("1.0", json.dumps(report, ensure_ascii=False, indent=2))
        self.diagnostic_text.configure(state="disabled")
        self.message.set("只读检查已结束，列表通过也不证明转盘安全或采集接口可用。可保存诊断报告。" if report.get("kind") == "read_only_experiment_check"
                         else "采图失败方法已记录。可保存诊断报告；设备动作结束情况仍需现场确认，不要反复试拍。")

    def save_diagnostic(self):
        if self._rpc_report is None:
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".json", filetypes=[("诊断报告", "*.json")])
        if path:
            try:
                write_json(Path(path), self._rpc_report)
            except (OSError, ValueError):
                self.message.set("诊断报告保存失败，请检查目录权限。")
            else:
                self.message.set("诊断报告已保存；包含方法名和状态码，不包含令牌或请求体。")

    def idle(self):
        return not (self.app.workflow and self.app.workflow.busy)

    def connect_real(self):
        if not self.idle():
            return
        if self.demo_config:
            for group in self.fields.values():
                group["experiment"].set("")
                group["z_m"].set("")
        self.demo_config = False
        self.approved.set(False)
        self.loading_approved.set(False)
        self.app.connect_field()
        self.message.set("正在发起连接检查；连接建立后请先点「只读检查」或「读取当前 Z」，核对实验、倍率、Z 与 XY 后再确认参数。")

    def load_demo(self):
        if not self.idle():
            return
        self.app._replace_workflow(SimulatedMarkDevice(xy_enabled=self.app.xy_mode.get() != "no_xy"),
                                   simulated=True, validate=validate_recipe, fit=fit_demo)
        self.app.workflow.connect()
        self.demo_config = True
        for key, z in (("upper", "100"), ("lower", "200")):
            self.fields[key]["z_m"].set(z)
            self.fields[key]["experiment"].set("Demo " + key)
            self.fields[key]["half_range_m"].set("5")
        self.approved.set(False)
        self.message.set("离线演示：所有图像均为模拟数据，不连接真实设备。核对参数并勾选确认后可试拍。")
        self.app.refresh_controls()

    def read_z(self, key):
        try:
            self.app.workflow.read_mark_z(key)
        except (AttributeError, ValueError, RuntimeError) as error:
            self.message.set(str(error))

    def set_z(self, key, z, xy):
        self.fields[key]["z_m"].set(f"{z*1e6:.6f}")
        position = "XY：未配置 / 未校验" if xy is None else f"XY {xy[0]*1e6:.3f}, {xy[1]*1e6:.3f} µm"
        self.message.set(f"已读取{'上' if key == 'upper' else '下'} mark：Z {z*1e6:.6f} µm；{position}")

    def recipe(self, layers=("upper", "lower")):
        try:
            values = {}
            for key, var in self.common.items():
                # The XY tolerance entry is disabled in no-XY mode; ignore any
                # stale value left by a saved motorized-mode configuration.
                if key == "xy_tolerance_m" and self.app.xy_mode.get() == "no_xy":
                    values[key] = MarkRecipe.__dataclass_fields__["xy_tolerance_m"].default
                else:
                    values[key] = float(var.get()) * (1e-6 if key.endswith('_m') else 1)
            selected = {}
            for key, group in self.fields.items():
                selected[key] = (MarkLayer(group["experiment"].get().strip(), float(group["z_m"].get())*1e-6,
                                          float(group["half_range_m"].get())*1e-6) if key in layers else None)
            recipe = MarkRecipe(**selected, **values, xy_mode=self.app.xy_mode.get(),
                                capture_kind="confocal" if self.capture_kind.get().startswith("共聚焦") else "camera_snap",
                                use_autofocus=self.use_af.get())
            recipe.validate(layers)
            return recipe
        except (ValueError, TypeError, OverflowError) as error:
            raise ValueError(f"请检查 Z、实验名、范围和检查参数：{error}") from None

    def capture(self, layers):
        try:
            workflow = self.app.workflow
            if workflow is None:
                raise ValueError("请先连接现场设备或载入离线演示")
            if self.demo_config and not workflow.simulated:
                raise ValueError("演示配置不能用于真实设备，请重新连接并示教现场参数")
            if not self.output.get().strip():
                raise ValueError("请选择图像输出目录")
            recipe = self.recipe(layers)
            workflow.start_mark_capture(recipe, Path(self.output.get()), layers=layers,
                                        confirmed=self.approved.get(), experiment_loading_confirmed=self.loading_approved.get())
            self.app.field_verified = False
            self.app.clear_result()
            self.message.set("采图任务已提交；请等待完成，期间不要手动改变 XY、物镜或倍率。")
            self.app.refresh_controls()
        except (ValueError, RuntimeError, OSError) as error:
            self.message.set(str(error))

    def browse(self):
        path = filedialog.askdirectory(parent=self)
        if path:
            self.output.set(path)

    def save_config(self):
        try:
            value = {**self.recipe().to_dict(), "simulated_configuration": self.demo_config}
            path = filedialog.asksaveasfilename(parent=self, defaultextension=".json", filetypes=[("采图配置", "*.json")])
            if path:
                write_json(Path(path), value)
                self.message.set("采图配置已保存；加载后仍需重新确认当前样品和 Z 路径。")
        except (OSError, ValueError) as error:
            self.message.set(str(error))

    def load_config(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[("采图配置", "*.json")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            recipe = MarkRecipe.from_dict(data)
            disconnected = False
            if recipe.xy_mode != self.app.xy_mode.get():
                self.app.xy_mode.set(recipe.xy_mode)
                self.app.change_device_mode()
                disconnected = True
            self.capture_kind.set("共聚焦采集（ZEN 采集实验）" if recipe.capture_kind == "confocal" else "相机拍照（ZEN 定位 / Snap）")
            self.use_af.set(recipe.use_autofocus)
            for key, group in self.fields.items():
                layer = getattr(recipe, key)
                group["experiment"].set("" if layer is None else layer.experiment)
                group["z_m"].set("" if layer is None else f"{layer.z_m*1e6:.9g}")
                group["half_range_m"].set("5" if layer is None else f"{layer.half_range_m*1e6:.9g}")
            for key, var in self.common.items():
                var.set(f"{getattr(recipe, key)*(1e6 if key.endswith('_m') else 1):.9g}")
            self.demo_config = data.get("simulated_configuration") is not False
            self.approved.set(False)
            self.loading_approved.set(False)
            self.message.set("配置已加载，XY 模式已切换且原连接已断开，请重新连接后再核对。" if disconnected
                             else "配置已加载。请重新核对当前 XY、物镜、两层 Z 和间隙，再勾选确认。")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            self.message.set("采图配置读取失败，请检查文件版本、单位和参数。")

    def open_output(self):
        workflow = self.app.workflow
        path = workflow.mark_output_path if workflow else None
        if path and path.is_dir():
            if hasattr(os, "startfile"):
                os.startfile(str(path))
            else:
                subprocess.Popen(["xdg-open", str(path)])

    def recover(self):
        simulated = bool(self.app.workflow and self.app.workflow.simulated)
        candidates = ((self.app.recover, self.app.recovery_runs),
                      (self.app.recover_field, self.app.field_recovery_runs))
        if not simulated:
            candidates = candidates[::-1]
        for action, pending in candidates:
            if pending:
                action(parent=self)
                break
        else:
            self.message.set("没有待确认的恢复记录")
            return
        self.message.set(self.app.status.get())
        self.approved.set(False)

    def refresh(self):
        if self.closed:
            return
        workflow = self.app.workflow
        busy = bool(workflow and workflow.busy)
        ready = bool(workflow and not busy and workflow.state in {"就绪", "完成", "已中止"})
        simulated = bool(workflow and workflow.simulated)
        no_xy = self.app.xy_mode.get() == "no_xy"
        self.mode.set(f"{'离线演示' if simulated else '真实设备'}：{workflow.state if workflow else '未连接'} · {'无 XY 单点采图' if no_xy else '有电动 XY'}")
        self.xy_note.set("XY 未配置、未校验，须手动固定样品；仅控制 Z 和保存 TIFF，不能进行角度扫描。" if no_xy else "当前 XY 不主动移动，软件校验 XY 回读；图像保存为 TIFF 后交给 SOMA 分析。")
        for entry in self.editable:
            entry.configure(state="disabled" if busy else "normal")
        for button in (*self.mode_controls, self.af_control, self.loading_approval_button, self.connect_button, self.demo_button, self.browse_button, self.load_button, self.save_button, self.approval_button):
            button.configure(state="disabled" if busy else "normal")
        self.inspect_button.configure(state="normal" if workflow and not busy and workflow.state != "未连接" else "disabled")
        self.diagnostic_save_button.configure(state="normal" if self._rpc_report and not busy else "disabled")
        self.kind_combo.configure(state="disabled" if busy else "readonly")
        self.common_entries["xy_tolerance_m"].configure(state="disabled" if busy or no_xy else "normal")
        for button in (*self.read_buttons, *self.capture_buttons):
            button.configure(state="normal" if ready else "disabled")
        self.stop_button.configure(state="normal" if busy else "disabled")
        pending = self.app.recovery_runs if simulated else self.app.field_recovery_runs
        self.recover_button.configure(state="normal" if pending and not busy else "disabled")
        self.open_button.configure(state="normal" if workflow and workflow.mark_output_path and not busy else "disabled")
        if workflow and workflow.mark_run_path and self.app.status.get() != self.last_app_status:
            self.last_app_status = self.app.status.get()
            self.message.set(self.last_app_status)
        self._refresh_job = self.after(100, self.refresh)

    def close(self):
        if not self.idle():
            self.app.stop()
            self.message.set("已请求停止；请等待设备调用返回和记录保存后再关闭。")
            return
        self.destroy()

    def destroy(self):
        # Cancel on the widget that owns the Tcl callback. Cancelling a child's
        # timer through the root would leave a stale command in the child and
        # cause errors on close/reopen or when shutting down the application.
        self.closed = True
        save_job = getattr(self, "_save_job", None)
        if save_job:
            self.after_cancel(save_job)
            self._save_job = None
        self.persist_preferences()
        for variable, trace in self._parameter_traces:
            variable.trace_remove("write", trace)
        self._parameter_traces.clear()
        job = getattr(self, "_refresh_job", None)
        if job:
            self.after_cancel(job)
            self._refresh_job = None
        super().destroy()
