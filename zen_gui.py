"""Tkinter workbench for the shared measurement workflow."""

import csv
import json
import os
import webbrowser
from math import isfinite
from dataclasses import replace
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from matplotlib import rcParams
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

from zen_fit import DEMO_CONVENTION, FIELD_CONVENTION, fit_demo, fit_field, validate_recipe
from zen_hardware.gateway import GatewayDevice, secure_channel
from zen_hardware.local_config import gateway_settings
from zen_hardware.preflight import check_gateway
from zen_hardware.simulator import SimulatedDevice
from zen_records import confirm_recovery, pending_recovery, write_json
from zen_recipe import load_points, load_recipe
from zen_path_editor import PathEditor
from zen_workflow import Recipe, Workflow

rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False
rcParams.update({
    "figure.facecolor": "#ffffff", "axes.facecolor": "#ffffff",
    "axes.edgecolor": "#d8dee4", "axes.linewidth": 0.8,
    "axes.labelcolor": "#5b6672", "axes.labelsize": 9,
    "grid.color": "#edf0f3", "grid.linewidth": 0.7,
    "xtick.color": "#9aa4ae", "ytick.color": "#9aa4ae",
    "xtick.labelsize": 8, "ytick.labelsize": 8,
})


DEMO_POINTS = ((0., 0.), (.001, 0.), (.001, .001), (0., .001))
WORKSPACE = "#f5f6f8"
PAPER = "#ffffff"
CARD_LINE = "#e6ebf1"
INK = "#14202c"
MUTED = "#7e8893"
ACCENT = "#2f6db0"
ACCENT_SOFT = "#eaf3fd"
DANGER = "#dc2626"
OK = "#1f7a4d"
WARN = "#b45309"


def card(parent):
    frame = tk.Frame(parent, bg=PAPER, highlightthickness=1, highlightbackground=CARD_LINE)
    inner = tk.Frame(frame, bg=PAPER)
    inner.pack(fill="both", expand=True, padx=14, pady=10)
    return frame, inner


class Workbench:
    def __init__(self, root, data_root):
        self.root, self.data_root = root, data_root
        from zen_preferences import Preferences
        self.preferences = Preferences(data_root / "preferences" / "ui.json")
        self.workflow = None
        self.gateway_overrides = dict(self.preferences.data.get("connection", {}))
        self.xy_mode = tk.StringVar(value=self.preferences.data.get("xy_mode", "motorized"))
        self._connected_mode = self.xy_mode.get()
        self.pending = None
        self.field_recipe = None
        self.field_verified = False
        self.verified_xy = None
        self.current_xy = None
        self.active_index = None
        self.point_status = {}
        self.recovery_runs = pending_recovery(data_root, simulated=True)
        self.field_recovery_runs = pending_recovery(data_root, simulated=False)
        root.title("ZEN AF 表面测角 2.4.0")
        root.geometry("1440x900")
        root.minsize(1120, 700)
        root.option_add("*Font", "{Microsoft YaHei UI} 10")
        self._style(root)
        header = tk.Frame(root, bg=PAPER, highlightthickness=1, highlightbackground=CARD_LINE)
        header.pack(fill="x")
        brand = tk.Frame(header, bg=ACCENT, width=10, height=10)
        brand.pack_propagate(False)
        brand.pack(side="left", padx=(18, 9), pady=15)
        tk.Label(header, text="ZEN AF 表面测角", bg=PAPER, fg=INK,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        self.mode_badge = tk.Label(header, text="真实设备", bg="#dce8f9", fg="#10244a",
                                   padx=9, pady=3, font=("Microsoft YaHei UI", 9, "bold"))
        self.mode_badge.pack(side="left", padx=14)
        self.heading = tk.StringVar(value="设备：未连接")
        tk.Label(header, textvariable=self.heading, bg=PAPER, fg=MUTED,
                 font=("Microsoft YaHei UI", 10)).pack(side="left", padx=6)
        engineering = ttk.Menubutton(header, text="演示与测试")
        engineering.pack(side="right", padx=(4, 16))
        self.engineering_menu = tk.Menu(engineering, tearoff=False)
        engineering.configure(menu=self.engineering_menu)
        self.demo = ttk.Button(header, text="正常演示", command=lambda: self.launch(None))
        self.fault = ttk.Button(header, text="第 3 点 AF 超时演示", command=lambda: self.launch("af_timeout"))
        self.single = ttk.Button(header, text="单点 AF 演示", command=lambda: self.launch(None, single_point=True))
        self.recover_button = ttk.Button(header, text="确认演示恢复", command=self.recover)
        self.menu_buttons = (self.demo, self.fault, self.single, self.recover_button)
        for button in self.menu_buttons:
            self.engineering_menu.add_command(label=button.cget("text"), command=button.invoke)

        footer = tk.Frame(root, bg=PAPER, highlightthickness=1, highlightbackground=CARD_LINE)
        footer.pack(side="bottom", fill="x")
        self.status = tk.StringVar(value="演示状态待确认" if self.recovery_runs else "可先新建配方、编辑路径，或在「演示与测试」中运行演示")
        self.export_button = ttk.Button(footer, text="导出结果", command=self.export)
        self.export_button.pack(side="right", padx=14, pady=6)
        self.stop_button = ttk.Button(footer, text="停止后续采集", state="disabled", command=self.stop)
        self.stop_button.pack(side="right")
        self.progress = ttk.Progressbar(footer, maximum=len(DEMO_POINTS), length=110)
        self.progress.pack(side="right", padx=10)
        status_label = tk.Label(footer, textvariable=self.status, bg=PAPER, fg=INK,
                                justify="left", anchor="w", pady=8)
        status_label.pack(side="left", fill="x", expand=True, padx=16)
        status_label.bind("<Configure>", lambda event: status_label.configure(
            wraplength=max(1, event.width - 4)))

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        sidebar_shell = tk.Frame(body, bg=WORKSPACE, width=352)
        sidebar_shell.pack(side="left", fill="y")
        sidebar_shell.pack_propagate(False)
        self.inspector_view = tk.Canvas(sidebar_shell, bg=WORKSPACE, highlightthickness=0)
        scroll = ttk.Scrollbar(sidebar_shell, orient="vertical", command=self.inspector_view.yview)
        scroll.pack(side="right", fill="y")
        self.inspector_view.pack(side="left", fill="both", expand=True)
        self.inspector_view.configure(yscrollcommand=scroll.set)
        inspector = ttk.Frame(self.inspector_view, padding=(12, 8, 6, 8))
        window = self.inspector_view.create_window(0, 0, window=inspector, anchor="nw")
        inspector.bind("<Configure>", lambda event: self.inspector_view.configure(
            scrollregion=self.inspector_view.bbox("all")))
        self.inspector_view.bind("<Configure>", lambda event: self.inspector_view.itemconfigure(
            window, width=event.width))
        self.step_badges = {}

        def step(number, title):
            group = tk.Frame(inspector, bg=PAPER, highlightthickness=1, highlightbackground=CARD_LINE)
            group.pack(fill="x", pady=(4, 8))
            head = tk.Frame(group, bg=PAPER)
            head.pack(fill="x", padx=12, pady=(9, 1))
            badge = tk.Label(head, text=number, bg="#eef2f6", fg="#5b6672", width=2,
                             font=("Segoe UI", 9, "bold"))
            badge.pack(side="left")
            self.step_badges[number] = badge
            tk.Label(head, text=title, bg=PAPER, fg=INK,
                     font=("Microsoft YaHei UI", 11, "bold")).pack(side="left", padx=8)
            inner = tk.Frame(group, bg=PAPER)
            inner.pack(fill="x", padx=12, pady=(2, 10))
            return inner

        def row(parent):
            frame = tk.Frame(parent, bg=PAPER)
            frame.pack(fill="x", pady=4)
            return frame

        connection = step("1", "设备连接")
        self.mode_buttons = []
        for label, value in (("有电动 XY · 角度扫描 / 采图", "motorized"), ("无电动 XY · 仅单点采图", "no_xy")):
            button = ttk.Radiobutton(connection, text=label, variable=self.xy_mode,
                                     value=value, command=self.change_device_mode,
                                     style="Card.TRadiobutton")
            button.pack(anchor="w", pady=2)
            self.mode_buttons.append(button)
        actions = row(connection)
        self.connect_button = ttk.Button(actions, text="Gateway 只读 XY", command=self.connect_field,
                                         style="Accent.TButton")
        self.connect_button.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.disconnect_button = ttk.Button(actions, text="断开", command=self.disconnect)
        self.disconnect_button.pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="连接设置", command=self.configure_gateway).pack(side="right")
        actions = row(connection)
        self.xy = tk.StringVar(value="XY  —")
        tk.Label(actions, textvariable=self.xy, bg=PAPER, fg=INK,
                 font=("Consolas", 10)).pack(side="left")
        self.field_recover = ttk.Button(actions, text="确认现场恢复", command=self.recover_field)
        self.field_recover.pack(side="right")
        ttk.Button(connection, text="硬件库 · 通信与单步测试", command=self.open_hardware_tests).pack(fill="x", pady=(4, 2))
        self.preflight_button = ttk.Button(connection, text="开机检查", command=self.open_preflight)
        self.preflight_button.pack(fill="x", pady=2)

        recipe = step("2", "测量配方")
        self.recipe_button = ttk.Button(recipe, text="加载现场配方", command=self.choose_recipe)
        self.recipe_button.pack(fill="x", pady=(2, 6))
        actions = row(recipe)
        self.edit_recipe_button = ttk.Button(actions, text="新建／编辑配方", command=self.edit_recipe)
        self.edit_recipe_button.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.save_recipe_button = ttk.Button(actions, text="保存现场配方", command=self.save_recipe)
        self.save_recipe_button.pack(side="right", fill="x", expand=True)
        self.recipe_info = tk.StringVar(value="现场配方未加载")
        recipe_text = ttk.Label(recipe, textvariable=self.recipe_info, style="Muted.TLabel")
        recipe_text.pack(anchor="w", pady=(2, 0))
        recipe_text.bind("<Configure>", lambda event: recipe_text.configure(
            wraplength=max(1, event.width)))

        measurement = step("3", "采集测量")
        self.path_button = ttk.Button(measurement, text="编辑点位路径", command=self.edit_path)
        self.path_button.pack(fill="x", pady=(2, 3))
        self.import_button = ttk.Button(measurement, text="从 CSV 导入点位（可选）", command=self.choose_points)
        self.import_button.pack(fill="x", pady=(0, 6))
        self.field_batch = ttk.Button(measurement, text="开始批量测角", command=self.run_field_batch,
                                      style="Accent.TButton")
        self.field_batch.pack(fill="x", pady=3)
        self.field_af = ttk.Button(measurement, text="当前点 AF", command=self.run_field_af)
        self.field_af.pack(fill="x", pady=3)
        point_tools = tk.Frame(measurement, bg=PAPER)
        ttk.Button(measurement, text="点位管理 ▾", command=lambda: (
            point_tools.pack_forget() if point_tools.winfo_manager() else point_tools.pack(fill="x")
        )).pack(anchor="w", pady=(6, 0))
        actions = row(point_tools)
        self.teach_button = ttk.Button(actions, text="示教当前 XY", command=self.teach)
        self.teach_button.pack(side="left")
        self.delete_button = ttk.Button(actions, text="删除选中点", command=self.delete_point)
        self.delete_button.pack(side="right")
        self.points = tk.Listbox(point_tools, height=3, exportselection=False, bd=0,
                                 highlightthickness=1, highlightbackground=CARD_LINE,
                                 selectbackground=ACCENT_SOFT, selectforeground=INK)
        self.points.pack(fill="x")

        images = step("4", "对位图像")
        self.mark_button = ttk.Button(images, text="上下 mark 采图", command=self.open_mark_capture)
        self.mark_button.pack(fill="x", pady=3)
        ttk.Label(images, text="当前点 · 上下层 TIFF · 交给 SOMA 分析",
                  style="Muted.TLabel").pack(anchor="w")

        main = ttk.Frame(body, padding=(16, 14, 16, 0))
        main.pack(side="left", fill="both", expand=True)

        metrics_row = ttk.Frame(main)
        metrics_row.pack(fill="x", pady=(0, 12))
        self.kpi = [tk.StringVar(value="—") for _ in range(4)]
        for index, (label, unit, value) in enumerate(zip(
                ("Rx", "Ry", "RMS", "PV"), ("µrad", "µrad", "µm", "µm"), self.kpi)):
            tile, tile_inner = card(metrics_row)
            tile.pack(side="left", fill="both", expand=True, padx=(0 if index == 0 else 10, 0))
            tk.Label(tile_inner, text=label, bg=PAPER, fg=MUTED,
                     font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
            readings = tk.Frame(tile_inner, bg=PAPER)
            readings.pack(anchor="w", pady=(1, 0))
            tk.Label(readings, textvariable=value, bg=PAPER, fg=ACCENT if index < 2 else INK,
                     font=("Consolas", 19, "bold")).pack(side="left")
            tk.Label(readings, text=unit, bg=PAPER, fg=MUTED,
                     font=("Microsoft YaHei UI", 9)).pack(side="left", anchor="s", padx=(4, 0), pady=(0, 4))

        plot_panel, plot_frame = card(main)
        plot_panel.pack(fill="both", expand=True)
        head = tk.Frame(plot_frame, bg=PAPER)
        head.pack(fill="x")
        tk.Label(head, text="XY 点位路径", bg=PAPER, fg=INK,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        self.plot_hint = tk.StringVar(value="")
        tk.Label(head, textvariable=self.plot_hint, bg=PAPER, fg=MUTED).pack(side="right")
        self.plot_fig = Figure(figsize=(5, 3.1), dpi=100, constrained_layout=True)
        self.plot_ax = self.plot_fig.add_subplot(111)
        self.plot_canvas = FigureCanvasTkAgg(self.plot_fig, master=plot_frame)
        toolbar = NavigationToolbar2Tk(self.plot_canvas, plot_frame, pack_toolbar=False)
        toolbar.configure(background=PAPER)
        for child in toolbar.winfo_children():
            try:
                child.configure(background=PAPER)
            except tk.TclError:
                pass
        toolbar.update()
        toolbar.pack(side="bottom", fill="x")
        self.plot_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.plot_annotation = None
        self.plot_fig.canvas.mpl_connect("motion_notify_event", self._plot_hover)
        self._reset_plot()

        detail_panel, detail = card(main)
        detail_panel.pack(fill="x", pady=(12, 0))
        tk.Label(detail, text="逐点明细", bg=PAPER, fg=INK,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        table_body = ttk.Frame(detail, style="Card.TFrame")
        table_body.pack(fill="both", expand=True, pady=(6, 0))
        columns = (
            ("point", "点位", 46), ("target", "目标 XY (µm)", 106),
            ("before", "AF 前 XY (µm)", 112), ("after", "AF 后 XY (µm)", 112),
            ("z", "AF Z (µm)", 74), ("state", "状态", 53), ("stage", "失败阶段", 82),
        )
        self.table = ttk.Treeview(table_body, columns=[c[0] for c in columns], show="headings", height=4)
        for key, title, width in columns:
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=width, anchor="center")
        self.table.tag_configure("failed", foreground="#973c39", background="#fbeceb")
        self.table.tag_configure("ok", foreground=OK)
        self.table.tag_configure("stopped", foreground=WARN)
        self.table.tag_configure("stripe", background="#f7f8fa")
        vertical = ttk.Scrollbar(table_body, orient="vertical", command=self.table.yview)
        horizontal = ttk.Scrollbar(table_body, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        horizontal.pack(side="bottom", fill="x")
        vertical.pack(side="right", fill="y")
        self.table.pack(side="left", fill="both", expand=True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        if self.xy_mode.get() == "no_xy":
            self.xy.set("XY：未配置 / 未校验")
            self.heading.set("设备：未连接 · 无 XY 单点采图")
        if self.preferences.warning:
            self.status.set(self.preferences.warning)
        self.refresh_controls()
        root.after(40, self.poll)

    def _style(self, root):
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background=WORKSPACE)
        style.configure("TLabel", background=WORKSPACE, foreground=INK)
        style.configure("Card.TFrame", background=PAPER)
        style.configure("Card.TLabel", background=PAPER, foreground=INK)
        style.configure("Muted.TLabel", background=PAPER, foreground=MUTED)
        style.configure("Card.TRadiobutton", background=PAPER)
        style.configure("Card.TCheckbutton", background=PAPER)
        style.configure("TButton", background=PAPER, foreground=INK, padding=(9, 6),
                        bordercolor="#dde2e7", lightcolor=PAPER, darkcolor=PAPER)
        style.map("TButton", background=[("active", "#edf2f6")],
                  foreground=[("disabled", "#8a94a3")])
        style.configure("Accent.TButton", background=ACCENT, foreground="white", padding=(9, 6),
                        bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT)
        style.map("Accent.TButton", background=[("disabled", "#e8ecef"), ("active", "#255b93")],
                  bordercolor=[("disabled", "#dfe4e8")],
                  foreground=[("disabled", MUTED), ("!disabled", "white")])
        style.configure("Treeview", rowheight=28, background=PAPER, foreground=INK,
                        font=("Microsoft YaHei UI", 9), fieldbackground=PAPER, borderwidth=0)
        style.configure("Treeview.Heading", background="#f6f8fa", foreground=MUTED,
                        borderwidth=0, relief="flat", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#e9edf1")
        style.configure("TMenubutton", background=PAPER, foreground=INK)

    def _set_step(self, key, active, warn=False):
        badge = self.step_badges[key]
        if warn:
            badge.configure(bg="#fde8e8", fg=DANGER)
        elif active:
            badge.configure(bg=ACCENT, fg="white")
        else:
            badge.configure(bg="#eef2f6", fg="#5b6672")

    def change_device_mode(self):
        if self.workflow and self.workflow.busy:
            self.xy_mode.set(self._connected_mode)
            return
        self._drop_workflow()
        self._connected_mode = self.xy_mode.get()
        self._reset_plot()
        self.preferences.update(xy_mode=self.xy_mode.get())
        self.field_verified = False
        self.clear_result()
        self.table.delete(*self.table.get_children())
        self.xy.set("XY：未配置 / 未校验" if self.xy_mode.get() == "no_xy" else "XY  —")
        self.heading.set("设备：未连接 · 无 XY 单点采图" if self.xy_mode.get() == "no_xy" else "设备：未连接")
        self.status.set("模式已切换，请重新连接。无 XY 模式不能角度扫描，也不能校验水平漂移。")
        self.refresh_controls()

    def open_hardware_tests(self):
        if self.workflow and self.workflow.busy:return
        from zen_hardware_gui import HardwareTestWindow
        self.hardware_dialog = HardwareTestWindow(self)

    def open_mark_capture(self):
        if self.workflow and self.workflow.busy:
            return
        existing = getattr(self, "mark_dialog", None)
        if existing and existing.winfo_exists():
            existing.lift()
            return
        from zen_mark_gui import MarkCaptureDialog
        self.mark_dialog = MarkCaptureDialog(self)

    def clear_result(self):
        for value in self.kpi:
            value.set("—")

    def release_gateway(self):
        if self.workflow and isinstance(self.workflow.device, GatewayDevice):
            self.workflow.device.close()

    def disconnect(self):
        if self.workflow is None or self.workflow.busy:
            return
        self._drop_workflow()
        no_xy = self.xy_mode.get() == "no_xy"
        self.heading.set("设备：未连接 · 无 XY 单点采图" if no_xy else "设备：未连接")
        self.status.set("已断开客户端通道；物理动作是否停止需在设备端确认。")
        self.refresh_controls()

    def _drop_workflow(self):
        """Discard the current workflow after delivering its queued events."""
        old, self.workflow = self.workflow, None
        self.pending = None
        if old is not None:
            self._drain_events(old)
            if isinstance(old.device, GatewayDevice):
                old.device.close()
        self.current_xy = None
        self.active_index = None
        self._redraw_plot()

    def _drain_events(self, workflow):
        """Deliver events still queued on a workflow being replaced."""
        try:
            while True:
                kind, *data = workflow.events.get_nowait()
                self._handle_event(workflow, kind, data)
        except Empty:
            pass

    def _replace_workflow(self, device, *, simulated, validate, fit):
        """Single entry point for swapping the bound device/connection."""
        self._drop_workflow()
        self.workflow = Workflow(device, self.data_root, simulated=simulated,
                                 validate=validate, fit=fit)
        self._connected_mode = self.xy_mode.get()
        self.field_verified = False
        self.verified_xy = None
        self.clear_result()

    def configure_gateway(self):
        if self.workflow and self.workflow.busy:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Gateway 连接设置")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="同机默认配置可留空。远程连接请填写地址、CA 和令牌。").grid(
            row=0, column=0, columnspan=3, pady=(0, 10), sticky="w")
        fields = {}
        for row, (key, label) in enumerate((
            ("ZEN_GATEWAY_ADDRESS", "地址与端口"),
            ("ZEN_GATEWAY_CA", "CA 证书路径"),
            ("ZEN_GATEWAY_TOKEN", "控制令牌"),
        ), 1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
            value = tk.StringVar(value=self.gateway_overrides.get(key, ""))
            fields[key] = value
            ttk.Entry(frame, width=42, textvariable=value,
                      show="*" if key.endswith("TOKEN") else "").grid(row=row, column=1, pady=3)

        def browse():
            path = filedialog.askopenfilename(parent=dialog, filetypes=[("PEM", "*.pem"), ("所有文件", "*")])
            if path:
                fields["ZEN_GATEWAY_CA"].set(path)

        def apply():
            self.gateway_overrides = {key: value.get().strip() for key, value in fields.items()
                                      if value.get().strip()}
            self.preferences.update(connection=self.gateway_overrides, xy_mode=self.xy_mode.get())
            dialog.destroy()
            self.connect_field()

        ttk.Button(frame, text="选择", command=browse).grid(row=2, column=2)
        ttk.Button(frame, text="只读测试连接", command=apply).grid(row=4, column=1, pady=8, sticky="e")
        ttk.Label(frame, text="填写值仅在本次运行有效。").grid(row=5, column=0, columnspan=3, sticky="w")
        dialog.grab_set()

    def open_preflight(self):
        if self.workflow and self.workflow.busy:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("开机检查")
        dialog.transient(self.root)
        panel = ttk.Frame(dialog, padding=16)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="开机检查", font=("Microsoft YaHei UI", 15, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        rows = {}
        for index, (key, title) in enumerate((
            ("address", "Gateway 地址"), ("ca", "CA 证书文件"), ("token", "控制令牌"),
            ("gateway", "Gateway 只读 Z" if self.xy_mode.get() == "no_xy" else "Gateway 只读 XY"), ("stage", "载物台状态"),
            ("services", "已注册服务"), ("recipe", "测量配方"), ("license", "ZEN API 许可证"),
        ), 1):
            ttk.Label(panel, text=title, width=18).grid(row=index, column=0, sticky="nw", pady=5)
            status = ttk.Label(panel, width=13, text="— 待检查")
            status.grid(row=index, column=1, sticky="nw", pady=5)
            detail = ttk.Label(panel, wraplength=360, justify="left")
            detail.grid(row=index, column=2, sticky="w", pady=5)
            rows[key] = status, detail

        def update_row(key, state, message):
            status, detail = rows[key]
            color = "#29704a" if state.startswith("✓") else "#973c39" if state.startswith("✕") else MUTED
            status.configure(text=state, foreground=color)
            detail.configure(text=message)

        license_confirmed = tk.BooleanVar(dialog, False)

        def update_license():
            confirmed = license_confirmed.get()
            update_row("license", "✓ 已人工确认" if confirmed else "— 待人工确认",
                       "请在 ZEN 的许可界面确认 ZEN API 工具包已启用。许可状态由现场人员核对。")

        update_license()
        ttk.Checkbutton(panel, text="我已在 ZEN 确认 API 许可", variable=license_confirmed,
                        command=update_license).grid(row=9, column=2, sticky="w", pady=6)
        controls = ttk.Frame(panel)
        controls.grid(row=10, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(controls, text="蔡司官方排查文档", command=lambda: webbrowser.open(
            "https://github.com/zeiss-microscopy/OAD/blob/master/ZEN-API/README.md#troubleshooting"
        )).pack(side="left", padx=(0, 10))
        close = ttk.Button(controls, text="关闭", command=dialog.destroy)
        close.pack(side="right", padx=(8, 0))

        def run_check():
            env = {**os.environ, **self.gateway_overrides}
            xy_enabled = self.xy_mode.get() != "no_xy"
            updates = Queue()
            for key in ("address", "ca", "token", "gateway", "stage", "services"):
                update_row(key, "… 检查中", "")
            recipe = self.field_recipe
            if not xy_enabled:
                update_row("recipe", "— 不适用", "无 XY 模式仅单点采图，不要求角度配方。")
            elif recipe is None:
                update_row("recipe", "— 待配置", "加载或新建现场配方。")
            else:
                try:
                    validate_recipe(recipe)
                except ValueError as error:
                    update_row("recipe", "— 待配置", str(error))
                else:
                    update_row("recipe", "✓ 通过", f"{recipe.name} · {len(recipe.targets)} 个点，配方通过本地检查。")
            check.state(["disabled"])
            close.state(["disabled"])
            dialog.protocol("WM_DELETE_WINDOW", lambda: None)

            def worker():
                try:
                    check_gateway(env, lambda *result: updates.put(result), xy_enabled=xy_enabled)
                except Exception:
                    for key in ("address", "ca", "token", "gateway", "stage", "services"):
                        updates.put((key, "— 未完成", "请核对 Gateway 配置后重试。"))
                finally:
                    updates.put(None)

            def poll():
                try:
                    while True:
                        result = updates.get_nowait()
                        if result is None:
                            check.state(["!disabled"])
                            close.state(["!disabled"])
                            dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
                            return
                        update_row(*result)
                except Empty:
                    dialog.after(40, poll)

            Thread(target=worker, daemon=True).start()
            poll()

        check = ttk.Button(controls, text="检查", style="Accent.TButton", command=run_check)
        check.pack(side="right")
        dialog.grab_set()

    def connect_field(self):
        if self.workflow and self.workflow.busy:
            return
        try:
            address, pem_path, token = gateway_settings({**os.environ, **self.gateway_overrides})
        except ValueError as exc:
            self.status.set(str(exc))
            return

        def channel():
            return secure_channel(address, pem_path.read_bytes(), token)

        self.xy.set("XY  —")
        self.table.delete(*self.table.get_children())
        self.progress["value"] = 0
        self._replace_workflow(GatewayDevice(channel, xy_enabled=self.xy_mode.get() != "no_xy"),
                               simulated=False, validate=validate_recipe, fit=fit_field)
        self.show_field_heading()
        self.workflow.connect()
        self.refresh_controls()

    def _plot_points(self):
        if self.workflow and self.workflow.simulated:
            return DEMO_POINTS
        return self.field_recipe.targets if self.field_recipe else ()

    def _reset_plot(self):
        self.point_status = {}
        self.current_xy = None
        self.active_index = None
        self._redraw_plot()

    def _redraw_plot(self):
        ax = getattr(self, "plot_ax", None)
        if ax is None:
            return
        ax.clear()
        ax.set_xlabel("X (µm)")
        ax.set_ylabel("Y (µm)")
        ax.grid(True)
        for spine in ax.spines.values():
            spine.set_color("#d8dee4")
        points = self._plot_points()
        self.plot_hint.set("" if not points else f"{len(points)} 个点位 · 工具栏可平移缩放")
        if not points:
            ax.text(0.5, 0.5, "等待加载或示教 XY 点位", transform=ax.transAxes,
                    ha="center", va="center", color="#8a94a3", fontsize=10)
        else:
            xs = [x * 1e6 for x, _ in points]
            ys = [y * 1e6 for _, y in points]
            if len(points) > 1:
                ax.plot(xs, ys, linestyle=(0, (5, 4)), linewidth=1.4,
                        color="#9ab4ca", zorder=1)
            colors = [{"ok": OK, "failed": DANGER, "stopped": WARN}.get(
                self.point_status.get(index), ACCENT) for index in range(1, len(points) + 1)]
            ax.scatter(xs, ys, s=46, c=colors, edgecolors="white", linewidths=1.0, zorder=3)
            if self.active_index and 1 <= self.active_index <= len(points):
                ax.scatter([xs[self.active_index - 1]], [ys[self.active_index - 1]],
                           s=120, facecolors="none", edgecolors=INK, linewidths=1.4, zorder=4)
            for index, (x, y) in enumerate(zip(xs, ys), 1):
                ax.annotate(str(index), (x, y), textcoords="offset points",
                            xytext=(7, 6), fontsize=8, color="#5b6672", zorder=4)
            if self.current_xy and all(map(isfinite, self.current_xy)):
                ax.scatter([self.current_xy[0] * 1e6], [self.current_xy[1] * 1e6],
                           marker="+", s=170, c=DANGER, linewidths=1.8, zorder=5,
                           label="当前设备 XY")
                ax.legend(loc="upper right", fontsize=8, frameon=True)
            ax.set_aspect("equal", adjustable="datalim")
            ax.margins(0.12)
        self.plot_annotation = ax.annotate(
            "", xy=(0, 0), xytext=(12, 12), textcoords="offset points", fontsize=8,
            bbox=dict(boxstyle="round,pad=0.3", fc=PAPER, ec=CARD_LINE))
        self.plot_annotation.set_visible(False)
        self.plot_canvas.draw_idle()

    def _plot_hover(self, event):
        annotation = self.plot_annotation
        if annotation is None:
            return
        show = False
        points = self._plot_points()
        if event.inaxes is self.plot_ax and points and event.xdata is not None:
            xs = [x * 1e6 for x, _ in points]
            ys = [y * 1e6 for _, y in points]
            xlim, ylim = self.plot_ax.get_xlim(), self.plot_ax.get_ylim()
            xspan = max(abs(xlim[1] - xlim[0]), 1e-9)
            yspan = max(abs(ylim[1] - ylim[0]), 1e-9)
            best = min(range(len(xs)),
                       key=lambda i: ((xs[i] - event.xdata) / xspan) ** 2
                       + ((ys[i] - event.ydata) / yspan) ** 2)
            if (((xs[best] - event.xdata) / xspan) ** 2
                    + ((ys[best] - event.ydata) / yspan) ** 2) < 0.0009:
                index = best + 1
                state = {"ok": "完成", "failed": "失败", "stopped": "已中止"}.get(
                    self.point_status.get(index), "待测")
                annotation.xy = (xs[best], ys[best])
                annotation.set_text(
                    f"点 {index} · {state}\nX {xs[best]:.3f} µm   Y {ys[best]:.3f} µm")
                annotation.set_visible(True)
                show = True
        if not show:
            annotation.set_visible(False)
        self.plot_canvas.draw_idle()

    def show_field_heading(self, workflow=None):
        workflow = workflow or self.workflow
        name = self.field_recipe.name if self.field_recipe else "未加载配方"
        state = workflow.state
        if not workflow.xy_enabled:
            self.heading.set(f"设备：{state} · 无 XY 单点采图（XY 未校验）")
        else:
            self.heading.set(f"设备：{state}   |   配方：{name}")

    def choose_recipe(self):
        if self.workflow and self.workflow.busy:
            return
        path = filedialog.askopenfilename(filetypes=[("配方 JSON", "*.json")])
        if not path:
            return
        try:
            recipe = load_recipe(Path(path))
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            self.status.set("配方读取失败，请检查版本、字段和单位")
            return
        self.apply_field_recipe(recipe)

    def save_recipe(self):
        recipe = self.field_recipe
        if recipe is None or (self.workflow and self.workflow.busy):
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json",
                                           filetypes=[("现场配方", "*.json")])
        if not path:
            return
        try:
            destination = Path(path)
            if destination.resolve().is_relative_to(self.data_root.resolve()):
                self.status.set("请选择运行记录目录之外的位置")
                return
            write_json(destination, {
                "schema_version": 1, "coordinate_unit": "m",
                "recipe_name": recipe.name, "experiment_name": recipe.experiment,
                "point_order": recipe.targets, "xy_tolerance_m": recipe.tolerance_m,
                "af_timeout_s": recipe.af_timeout, "arrival_timeout_s": recipe.arrival_timeout,
                "sign_convention": recipe.sign_convention,
            })
        except (OSError, ValueError):
            self.status.set("保存配方失败，请检查目标目录及权限")
        else:
            self.status.set("现场配方已保存，下次可直接加载")

    def edit_recipe(self):
        if self.workflow and self.workflow.busy:
            return
        old = self.field_recipe
        dialog = tk.Toplevel(self.root)
        dialog.title("现场测量配方")
        dialog.transient(self.root)
        panel = ttk.Frame(dialog, padding=12)
        panel.pack()
        inputs = []
        for i, (label, default) in enumerate((
            ("配方名称", old.name if old else "现场配方"),
            ("ZEN 软件 AF 实验名", old.experiment if old else ""),
            ("XY 容差 (µm)", old.tolerance_m * 1e6 if old else 1),
            ("AF 超时 (s)", old.af_timeout if old else 3),
            ("到位超时 (s)", old.arrival_timeout if old else 2),
        )):
            ttk.Label(panel, text=label).grid(row=i, column=0, sticky="w")
            field = ttk.Entry(panel, width=32)
            field.insert(0, str(default))
            field.grid(row=i, column=1, padx=8, pady=3)
            inputs.append(field)
        ttk.Label(panel, text=FIELD_CONVENTION, wraplength=360).grid(row=5, columnspan=2, sticky="w")

        def apply():
            name, experiment, tolerance, af, arrival = [field.get().strip() for field in inputs]
            try:
                values = (float(tolerance) * 1e-6, float(af), float(arrival))
                if not name or not experiment or not all(isfinite(v) and v > 0 for v in values):
                    raise ValueError
            except ValueError:
                messagebox.showerror("配方无效", "请填写名称和实验名，容差及超时须为有限正数。", parent=dialog)
                return
            dialog.destroy()
            self.apply_field_recipe(Recipe(name, experiment, old.targets if old else (),
                                           *values, FIELD_CONVENTION))

        ttk.Button(panel, text="应用配方", command=apply).grid(row=6, column=1, sticky="e", pady=8)
        dialog.grab_set()

    def apply_field_recipe(self, recipe):
        self.field_recipe = recipe
        self.field_verified = False
        self.points.delete(0, "end")
        for index, (x, y) in enumerate(recipe.targets, 1):
            self.points.insert("end", f"{index}. {x*1e6:g}, {y*1e6:g} µm")
        self.recipe_info.set(f"{recipe.experiment}\nXY 容差 {recipe.tolerance_m*1e6:g} µm"
                             f"\nAF 超时 {recipe.af_timeout:g} s · {len(recipe.targets)} 点")
        if self.workflow and not self.workflow.simulated:
            self.workflow.invalidate()
            self.clear_result()
            self.table.delete(*self.table.get_children())
            self.show_field_heading()
        self.point_status.clear()
        self._redraw_plot()
        self.refresh_controls()

    def edit_path(self):
        if not self.field_recipe or (self.workflow and self.workflow.busy):
            return
        def apply(targets):
            self.apply_field_recipe(replace(self.field_recipe, targets=targets))
            self.status.set(f"路径已更新，共 {len(targets)} 个点；请保存现场配方，并重新验证当前点 AF")
        PathEditor(self.root, self.field_recipe.targets, apply)

    def choose_points(self):
        if not self.field_recipe or (self.workflow and self.workflow.busy):
            return
        path = filedialog.askopenfilename(filetypes=[("点位 CSV", "*.csv")])
        if not path:
            return
        try:
            points = load_points(Path(path))
        except ValueError as exc:
            self.status.set(str(exc))
            return
        except (OSError, csv.Error):
            self.status.set("点位 CSV 读取失败，请检查文件格式")
            return
        self.apply_field_recipe(replace(self.field_recipe, targets=points))
        self.status.set(f"已导入 {len(points)} 个点，按文件顺序采集")

    def teach(self):
        if self.field_ready() and self.field_recipe:
            self.workflow.teach_xy()
            self.refresh_controls()

    def delete_point(self):
        selection = self.points.curselection()
        if self.field_ready() and self.field_recipe and selection:
            targets = self.field_recipe.targets
            i = selection[0]
            self.apply_field_recipe(replace(self.field_recipe, targets=targets[:i] + targets[i+1:]))

    def apply_taught_point(self, workflow, xy):
        if self.workflow is not workflow:
            return
        if workflow.busy:
            self.root.after(40, self.apply_taught_point, workflow, xy)
            return
        recipe = self.field_recipe
        self.apply_field_recipe(replace(recipe, targets=recipe.targets + (xy,)))

    def run_field_af(self):
        workflow = self.workflow
        if not self.field_ready():
            return
        recipe = self.field_recipe
        if recipe is None:
            self.status.set("请先加载已核对的现场配方")
            return
        if not messagebox.askyesno(
            "确认当前点 AF",
            "软件 AF 将驱动 Z 轴。请确认当前物镜间隙、实验和 Z 搜索范围安全。",
        ):
            return
        self.table.delete(*self.table.get_children())
        self.point_status.clear()
        self._redraw_plot()
        self.field_verified = False
        self.clear_result()
        self.progress.configure(maximum=1, value=0)
        workflow.start(recipe, path_confirmed=True, single_point=True)
        self.refresh_controls()

    def field_ready(self):
        workflow = self.workflow
        return bool(workflow and workflow.xy_enabled and not workflow.simulated and not workflow.busy
                    and workflow.state in {"就绪", "完成", "已中止"})

    def run_field_batch(self):
        if not self.field_ready() or not self.field_verified or self.verified_xy is None:
            return
        recipe = self.field_recipe
        if recipe.sign_convention != FIELD_CONVENTION:
            self.status.set("请在配方中明确采用联调符号约定，V1.1 符号待核对")
            return
        try:
            validate_recipe(recipe)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        plan = "\n".join(f"{i}. X {x*1e6:.3f} µm   Y {y*1e6:.3f} µm"
                         for i, (x, y) in enumerate(recipe.targets, 1))
        start_xy = self.verified_xy
        plan = f"起点（单点 AF 后） X {start_xy[0]*1e6:.3f} µm   Y {start_xy[1]*1e6:.3f} µm\n" + plan
        dialog = tk.Toplevel(self.root)
        dialog.title("确认批量运动")
        dialog.transient(self.root)
        ttk.Label(dialog, text="核对实际起点到首点、各段路径与物镜间隙，确认后执行 XY 和 AF。",
                  padding=8).pack()
        text = tk.Text(dialog, width=65, height=min(16, len(recipe.targets) + 1), wrap="none")
        text.pack(fill="both", expand=True, padx=8)
        text.insert("1.0", plan)
        text.configure(state="disabled")
        def start():
            dialog.destroy()
            self.clear_result()
            self.table.delete(*self.table.get_children())
            self.point_status.clear()
            self._redraw_plot()
            self.progress.configure(maximum=len(recipe.targets), value=0)
            self.workflow.start(recipe, path_confirmed=True, confirmed_start=start_xy)
            self.refresh_controls()
        ttk.Button(dialog, text="确认并执行", command=start).pack(side="right", padx=8, pady=8)
        ttk.Button(dialog, text="取消", command=dialog.destroy).pack(side="right")
        dialog.grab_set()

    def launch(self, fault, *, single_point=False):
        if self.recovery_runs or (self.workflow and (
            self.workflow.busy or (
                self.workflow.simulated and self.workflow.state == "设备状态待确认"
            )
        )):
            return
        self.clear_result()
        self.xy.set("XY  —")
        self.table.delete(*self.table.get_children())
        self.progress["value"] = 0
        self.progress["maximum"] = 1 if single_point else len(DEMO_POINTS)
        self._replace_workflow(SimulatedDevice(fault=fault), simulated=True,
                               validate=validate_recipe, fit=fit_demo)
        recipe = Recipe("演示配方", "Demo AF", DEMO_POINTS, 1e-6, 3, 2, DEMO_CONVENTION)
        self.pending = (recipe, single_point)
        self.workflow.connect()
        self.heading.set("离线演示：连接检查")
        self._reset_plot()
        self.refresh_controls()

    def recover(self, parent=None):
        if not self.recovery_runs or (self.workflow and self.workflow.busy):
            return
        if not messagebox.askyesno("演示恢复", "模拟现场确认 XY 与 AF/Z 动作均已结束？",
                                   parent=parent or self.root):
            return
        try:
            if self.workflow and self.workflow.simulated:
                self.workflow.device.confirm_simulated_recovery()
            for run in self.recovery_runs:
                confirm_recovery(run, xy_confirmed=True, af_z_confirmed=True)
            self.recovery_runs = pending_recovery(self.data_root, simulated=True)
        except OSError:
            self.status.set("恢复记录保存失败，请检查数据目录")
        else:
            self._drop_workflow()
            self.heading.set("设备：未连接")
            self.status.set("演示恢复已记录，可重新运行")
        self.refresh_controls()

    def recover_field(self, parent=None):
        if not self.field_recovery_runs or (self.workflow and self.workflow.busy):
            return
        if not messagebox.askyesno(
            "独立确认 XY", "请按现场规程检查：有电动 XY 时确认其动作已结束；无电动 XY 时确认样品已固定、手动水平调整已结束。是否已确认？",
            parent=parent or self.root,
        ):
            return
        if not messagebox.askyesno(
            "独立确认 AF/Z",
            "请在 ZEN 或设备端独立确认软件 AF、Z 轴和拍摄均已结束。是否已确认？",
            parent=parent or self.root,
        ):
            return
        try:
            for run in self.field_recovery_runs:
                confirm_recovery(run, xy_confirmed=True, af_z_confirmed=True)
            self.field_recovery_runs = pending_recovery(self.data_root, simulated=False)
        except OSError:
            self.status.set("恢复记录保存失败，请检查数据目录")
            return
        self.field_verified = False
        self.status.set("现场恢复已记录，请重新连接并验证单点 AF")
        self.refresh_controls()

    def export(self):
        if not self.workflow or self.workflow.busy or self.workflow.result_path is None:
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                           filetypes=[("JSON", "*.json")])
        if path:
            try:
                run = self.workflow.result_path
                recipe = json.loads((run / "recipe.json").read_text(encoding="utf-8"))
                destination = Path(path)
                if destination.resolve().is_relative_to(self.data_root.resolve()):
                    self.status.set("请选择运行记录目录之外的导出位置")
                    return
                write_json(destination, {
                    **self.workflow.result, "run_id": run.name, "recipe": recipe,
                    "simulated": self.workflow.simulated,
                })
            except (OSError, ValueError):
                self.status.set("导出失败，请检查目标目录")
            else:
                self.status.set("结果已导出")

    def refresh_controls(self):
        workflow = self.workflow
        simulated = bool(workflow and workflow.simulated)
        self.mode_badge.configure(text="离线演示" if simulated else "真实设备",
                                  bg="#f08316" if simulated else "#dce8f9",
                                  fg="white" if simulated else "#10244a")
        busy = bool(workflow and workflow.busy)
        locked = bool(self.recovery_runs or (
            workflow and workflow.simulated and workflow.state == "设备状态待确认"
        ))
        for button in (self.demo, self.fault, self.single):
            button.configure(state="disabled" if busy or locked else "normal")
        self.recover_button.configure(state="normal" if self.recovery_runs and not busy else "disabled")
        for index, button in enumerate(self.menu_buttons):
            self.engineering_menu.entryconfigure(
                index, state="disabled" if button.instate(["disabled"]) else "normal")
        self.export_button.configure(
            state="normal" if workflow and workflow.result is not None and not busy else "disabled"
        )
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.mark_button.configure(state="disabled" if busy else "normal")
        self.connect_button.configure(state="disabled" if busy else "normal", text="Gateway 只读 Z" if self.xy_mode.get() == "no_xy" else "Gateway 只读 XY")
        self.disconnect_button.configure(state="normal" if workflow and not busy else "disabled")
        for button in self.mode_buttons:
            button.configure(state="disabled" if busy else "normal")
        self.preflight_button.configure(state="disabled" if busy else "normal")
        self.field_recover.configure(
            state="normal" if self.field_recovery_runs and not busy else "disabled"
        )
        field_ready = self.field_ready()
        self.field_af.configure(state="normal" if field_ready and self.field_recipe and not busy else "disabled")
        self.field_batch.configure(state="normal" if field_ready and self.field_verified else "disabled")
        for button in (self.teach_button, self.delete_button):
            button.configure(state="normal" if field_ready and self.field_recipe else "disabled")
        self.recipe_button.configure(state="disabled" if busy else "normal")
        self.edit_recipe_button.configure(state="disabled" if busy else "normal")
        self.save_recipe_button.configure(state="normal" if self.field_recipe and not busy else "disabled")
        self.import_button.configure(state="normal" if self.field_recipe and not busy else "disabled")
        self.path_button.configure(state="normal" if self.field_recipe and not busy else "disabled")
        self._set_step("1", bool(workflow and workflow.state in {"就绪", "完成", "已中止"}),
                       warn=bool(workflow and workflow.state == "设备状态待确认"))
        self._set_step("2", bool(self.field_recipe))
        self._set_step("3", busy or self.field_verified)
        self._set_step("4", bool(workflow and workflow.mark_output_path))

    def stop(self):
        if self.workflow:
            self.pending = None
            self.workflow.stop()

    def close(self):
        if self.workflow and self.workflow.busy:
            self.stop()
            messagebox.showinfo("任务仍在处理", "已请求停止后续采集。请等待本轮记录保存后关闭。")
            return
        self.release_gateway()
        self.root.destroy()

    def _handle_event(self, workflow, kind, data):
        if kind == "state":
            state, point, detail = data
            if workflow.simulated:
                self.heading.set(f"离线演示：{state}")
            else:
                self.show_field_heading(workflow)
            if not workflow.simulated and detail == "单点 AF 验证完成":
                self.field_verified = True
            message = f"{state} · 点位 {point or '—'} {detail}"
            if state == "设备状态待确认" and workflow.simulated:
                self.recovery_runs = pending_recovery(self.data_root, simulated=True)
            if state == "设备状态待确认" and not workflow.simulated:
                self.field_verified = False
                self.field_recovery_runs = pending_recovery(self.data_root, simulated=False)
                message += " · 请在设备端分别确认 XY 与 AF/Z，记录恢复后重新连接"
                message += " · 本轮无有效结果，请独立确认 XY 与 AF/Z"
            self.active_index = point or None
            self._redraw_plot()
            self.status.set(message)
        elif kind == "hardware_result":
            dialog = getattr(self, "hardware_dialog", None)
            if dialog and dialog.winfo_exists():dialog.show_result(data[0])
        elif kind == "mark_diagnostic":
            dialog = getattr(self, "mark_dialog", None)
            if dialog and dialog.winfo_exists():
                dialog.show_diagnostic(data[0])
        elif kind in {"mark_z", "mark_error"}:
            dialog = getattr(self, "mark_dialog", None)
            if kind == "mark_error":
                self.status.set(data[0])
                if dialog and dialog.winfo_exists():
                    dialog.message.set(data[0])
            elif dialog and dialog.winfo_exists():
                dialog.set_z(*data)
        elif kind == "verified_xy":
            self.verified_xy = data[0]
        elif kind == "z_only":
            self.xy.set(f"XY：未配置 / 未校验；Z {data[0]*1e6:.3f} µm")
        elif kind == "xy":
            x, y = data[0]
            self.current_xy = (x, y)
            self.xy.set(f"XY  {x*1e6:.3f}, {y*1e6:.3f} µm")
            self._redraw_plot()
        elif kind == "taught":
            self.apply_taught_point(workflow, data[0])
        elif kind == "stop":
            self.status.set(data[0])
        elif kind == "row":
            row = data[0]
            z = "—" if row.z is None else f"{row.z*1e6:.3f}"
            label = {"ok": "完成", "failed": "失败", "stopped": "已中止"}[row.status]
            def xy_text(xy):
                return "—" if xy is None else f"{xy[0]*1e6:.3f},{xy[1]*1e6:.3f}"

            item = self.table.insert("", "end", values=(
                row.index, xy_text(row.target), xy_text(row.before_af),
                xy_text(row.after_af), z, label, row.failure_stage or "—",
            ), tags=(("stripe" if row.index % 2 == 0 else ""), row.status))
            self.table.see(item)
            self.progress["value"] = row.index
            self.point_status[row.index] = row.status
            self._redraw_plot()
        elif kind == "result":
            r = data[0]
            for value, formatted in zip(self.kpi, (
                f"{r['rx_urad']:+.3f}", f"{r['ry_urad']:+.3f}",
                f"{r['rms_m']*1e6:.3f}", f"{r['pv_m']*1e6:.3f}",
            )):
                value.set(formatted)

    def poll(self):
        workflow = self.workflow
        if workflow:
            self._drain_events(workflow)
            if self.pending and not workflow.busy:
                (recipe, single_point), self.pending = self.pending, None
                if workflow.state == "就绪":
                    workflow.start(recipe, path_confirmed=True, single_point=single_point)
            self.refresh_controls()
        self.root.after(40, self.poll)


if __name__ == "__main__":
    root = tk.Tk()
    Workbench(root, Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ZENSurfaceTilt")
    root.mainloop()
