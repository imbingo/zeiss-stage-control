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
from zen_workflow import Workflow


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
        self.approved = tk.BooleanVar(value=False)
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
        ttk.Label(shell, textvariable=self.xy_note, wraplength=970).pack(anchor="w", pady=6)
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
        ttk.Label(shell, text="共聚焦请使用已在 ZEN 采集页验证过的单通道、单 Z 层、单时间点、无拼图实验；不会自动改拍相机图。", wraplength=970).pack(anchor="w", pady=3)
        self.af_control = ttk.Checkbutton(shell, text="定位后执行局部 AF（初次连机可取消，仅验证示教 Z 和采集保存）", variable=self.use_af)
        self.af_control.pack(anchor="w")
        ttk.Label(shell, text="在 ZEN 中手动调清楚对应 mark 后，点击“读取当前 Z”；上下层可使用不同曝光和照明的实验。", wraplength=920).pack(anchor="w", pady=6)
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
        ttk.Label(shell, textvariable=self.message, wraplength=940, justify="left").pack(fill="x", anchor="w", pady=8)
        ttk.Label(shell, text="完成后停留在最后拍摄层的焦面，不自动返回。停止/超时不等于物理动作已结束；异常时须确认 XY、Z、AF 与拍摄状态。", wraplength=940, foreground="#626873").pack(side="bottom", anchor="w", pady=8)
        for variable in [self.output, self.capture_kind, self.use_af, self.app.xy_mode, *self.common.values(), *(v for group in self.fields.values() for v in group.values())]:
            variable.trace_add("write", lambda *args: self.approved.set(False))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.grab_set()
        self._refresh_job = self.after(100, self.refresh)

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
        self.app.connect_field()
        self.message.set(self.app.status.get())

    def load_demo(self):
        if not self.idle():
            return
        self.app.release_gateway()
        self.app.pending = None
        self.app.field_verified = False
        self.app.clear_result()
        self.app.workflow = Workflow(SimulatedMarkDevice(xy_enabled=self.app.xy_mode.get() != "no_xy"), self.app.data_root, simulated=True,
                                     validate=validate_recipe, fit=fit_demo)
        self.app._connected_mode = self.app.xy_mode.get()
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
            values = {key: float(var.get()) * (1e-6 if key.endswith('_m') else 1)
                      for key, var in self.common.items()}
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
                                        confirmed=self.approved.get())
            self.app.field_verified = False
            self.app.clear_result()
            self.message.set("采图任务已提交；请等待完成，期间不要手动改变 XY、物镜或倍率。")
            self.app.refresh_controls()
        except (ValueError, RuntimeError) as error:
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
            if recipe.xy_mode != self.app.xy_mode.get():
                self.app.xy_mode.set(recipe.xy_mode)
                self.app.change_device_mode()
            self.capture_kind.set("共聚焦采集（ZEN 采集实验）" if recipe.capture_kind == "confocal" else "相机拍照（ZEN 定位 / Snap）")
            self.use_af.set(recipe.use_autofocus)
            for key, group in self.fields.items():
                layer = getattr(recipe, key)
                group["experiment"].set(layer.experiment)
                group["z_m"].set(f"{layer.z_m*1e6:.9g}")
                group["half_range_m"].set(f"{layer.half_range_m*1e6:.9g}")
            for key, var in self.common.items():
                var.set(f"{getattr(recipe, key)*(1e6 if key.endswith('_m') else 1):.9g}")
            self.demo_config = data.get("simulated_configuration") is not False
            self.approved.set(False)
            self.message.set("配置已加载。请重新核对当前 XY、物镜、两层 Z 和间隙，再勾选确认。")
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
        if self.app.workflow and self.app.workflow.simulated:
            self.app.recover()
        else:
            self.app.recover_field()
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
        for button in (*self.mode_controls, self.af_control, self.connect_button, self.demo_button, self.browse_button, self.load_button, self.save_button, self.approval_button):
            button.configure(state="disabled" if busy else "normal")
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
        job = getattr(self, "_refresh_job", None)
        if job:
            self.after_cancel(job)
            self._refresh_job = None
        super().destroy()
