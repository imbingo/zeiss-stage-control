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

from zen_fit import DEMO_CONVENTION, FIELD_CONVENTION, fit_demo, fit_field, validate_recipe
from zen_hardware.gateway import GatewayDevice, secure_channel
from zen_hardware.local_config import gateway_settings
from zen_hardware.preflight import check_gateway
from zen_hardware.simulator import SimulatedDevice
from zen_records import confirm_recovery, pending_recovery, write_json
from zen_recipe import load_points, load_recipe
from zen_path_editor import PathEditor
from zen_workflow import Recipe, Workflow


DEMO_POINTS = ((0., 0.), (.001, 0.), (.001, .001), (0., .001))
WORKSPACE = "#f2f1f0"
PAPER = "#ffffff"
INK = "#27313b"
MUTED = "#626873"
ACCENT = "#4779a6"
BORDER = "#e4e3e2"


def rounded_panel(parent):
    panel = tk.Frame(parent, bg=WORKSPACE)
    backdrop = tk.Canvas(panel, bg=WORKSPACE, highlightthickness=0)
    backdrop.place(relwidth=1, relheight=1)

    def draw_border(event):
        width, height = event.width - 1, event.height - 1
        radius = min(14, width / 2, height / 2)
        backdrop.delete("panel")
        backdrop.create_polygon(
            radius, 1, width-radius, 1, width, 1, width, radius,
            width, height-radius, width, height, width-radius, height,
            radius, height, 1, height, 1, height-radius, 1, radius, 1, 1,
            smooth=True, splinesteps=16, fill=PAPER, outline=BORDER, tags="panel",
        )

    backdrop.bind("<Configure>", draw_border)
    content = tk.Frame(panel, bg=PAPER)
    content.pack(fill="both", expand=True, padx=12, pady=9)
    return panel, content


class Workbench:
    def __init__(self, root, data_root):
        self.root, self.data_root = root, data_root
        self.workflow = None
        self.gateway_overrides = {}
        self.pending = None
        self.field_recipe = None
        self.field_verified = False
        self.verified_xy = None
        self.recovery_runs = pending_recovery(data_root, simulated=True)
        self.field_recovery_runs = pending_recovery(data_root, simulated=False)
        root.title("ZEN AF 表面测角 2.0.4")
        root.geometry("1568x953")
        root.minsize(960, 680)
        root.option_add("*Font", "{Microsoft YaHei UI} 10")
        ink, blue, surface, line = INK, ACCENT, WORKSPACE, BORDER
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background=surface)
        style.configure("TLabel", background=surface, foreground=ink)
        style.configure("Paper.TFrame", background=PAPER)
        style.configure("TButton", background=PAPER, foreground=ink, padding=(8, 6),
                        bordercolor="#deddda", lightcolor=PAPER, darkcolor=PAPER)
        style.map("TButton", background=[("active", "#edf2f6")], foreground=[("disabled", "#787d83")])
        style.configure("Accent.TButton", background=blue, foreground="white", padding=(8, 6),
                        bordercolor=blue, lightcolor=blue, darkcolor=blue)
        style.map("Accent.TButton", background=[("disabled", "#e8ecef"), ("active", "#39668e")],
                  bordercolor=[("disabled", "#dfe4e8")], foreground=[("disabled", MUTED), ("!disabled", "white")])
        style.configure("Treeview", rowheight=28, background=PAPER, foreground=ink,
                        font=("Microsoft YaHei UI", 9),
                        fieldbackground=PAPER, borderwidth=0)
        style.configure("Treeview.Heading", background="#f5f4f3", foreground=MUTED,
                        borderwidth=0, relief="flat", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Horizontal.TProgressbar", background=blue, troughcolor="#e6e5e3")
        shell = ttk.Frame(root)
        shell.pack(fill="both", expand=True)
        header = tk.Frame(shell, bg="#faf9f8", highlightthickness=1, highlightbackground=line)
        header.pack(fill="x")
        self.mode_badge = tk.Label(header, text="现场", bg="#dce8f9", fg=ink, padx=10, pady=4,
                                   font=("Microsoft YaHei UI", 10, "bold"))
        self.mode_badge.pack(side="left", padx=(26, 0))
        self.heading = tk.StringVar(value="未连接   |   工程模式")
        tk.Label(header, textvariable=self.heading, bg="#faf9f8", fg=ink,
                 font=("Microsoft YaHei UI", 13, "bold"), pady=13).pack(side="left", padx=15)
        engineering = ttk.Menubutton(header, text="工程工具")
        engineering.pack(side="left", padx=15)
        menu = tk.Menu(engineering, tearoff=False)
        engineering.configure(menu=menu)
        self.demo = ttk.Button(header, text="正常演示", command=lambda: self.launch(None))
        self.fault = ttk.Button(header, text="第 3 点 AF 超时演示", command=lambda: self.launch("af_timeout"))
        self.single = ttk.Button(header, text="单点 AF 演示", command=lambda: self.launch(None, single_point=True))
        self.recover_button = ttk.Button(header, text="确认演示恢复", command=self.recover)
        for button in (self.demo, self.fault, self.single, self.recover_button):
            menu.add_command(label=button.cget("text"), command=button.invoke)
        footer = tk.Frame(shell, bg="#faf9f8", highlightthickness=1, highlightbackground=line)
        footer.pack(side="bottom", fill="x", pady=(10, 0))
        self.status = tk.StringVar(value="演示状态待确认" if self.recovery_runs else "请选择演示场景")
        self.export_button = ttk.Button(footer, text="导出结果", command=self.export)
        self.export_button.pack(side="right", padx=14)
        self.stop_button = ttk.Button(footer, text="停止后续采集", state="disabled", command=self.stop)
        self.stop_button.pack(side="right")
        self.progress = ttk.Progressbar(footer, maximum=len(DEMO_POINTS), length=96)
        self.progress.pack(side="right", padx=10)
        status_label = tk.Label(footer, textvariable=self.status, bg="#faf9f8", fg=ink,
                                justify="left", anchor="w", pady=9)
        status_label.pack(side="left", fill="x", expand=True, padx=16)
        status_label.bind("<Configure>", lambda event: status_label.configure(
            wraplength=max(1, event.width - 4)))

        body = ttk.Frame(shell)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=7, uniform="workbench")
        body.grid_columnconfigure(1, weight=3, uniform="workbench")
        body.grid_rowconfigure(0, weight=1)
        main = ttk.Frame(body, padding=(24, 17, 18, 0))
        main.grid(row=0, column=0, sticky="nsew")
        inspector_shell = tk.Frame(body, bg=surface, highlightthickness=1, highlightbackground=line)
        inspector_shell.grid(row=0, column=1, sticky="nsew", padx=(0, 12), pady=(12, 0))
        self.inspector_view = tk.Canvas(inspector_shell, bg=surface, highlightthickness=0)
        scroll = ttk.Scrollbar(inspector_shell, orient="vertical", command=self.inspector_view.yview)
        scroll.pack(side="right", fill="y")
        self.inspector_view.pack(side="left", fill="both", expand=True)
        self.inspector_view.configure(yscrollcommand=scroll.set)
        inspector = ttk.Frame(self.inspector_view, padding=(10, 4))
        window = self.inspector_view.create_window(0, 0, window=inspector, anchor="nw")
        inspector.bind("<Configure>", lambda event: self.inspector_view.configure(
            scrollregion=self.inspector_view.bbox(window)))
        self.inspector_view.bind("<Configure>", lambda event: self.inspector_view.itemconfigure(
            window, width=event.width))

        def section(number, title):
            group = ttk.Frame(inspector)
            group.pack(fill="x", pady=(8, 6))
            banner = ttk.Frame(group)
            banner.pack(fill="x", pady=(0, 7))
            tk.Label(banner, text=number, bg=surface, fg=MUTED,
                     font=("Segoe UI", 10, "bold")).pack(side="left", padx=(2, 9))
            ttk.Label(banner, text=title,
                      font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
            return group

        def row(parent):
            frame = ttk.Frame(parent)
            frame.pack(fill="x", pady=4)
            return frame

        connection = section("01", "设备连接")
        actions = row(connection)
        self.connect_button = ttk.Button(actions, text="Gateway 只读 XY", command=self.connect_field)
        self.connect_button.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(actions, text="连接设置", command=self.configure_gateway).pack(side="right")
        actions = row(connection)
        self.xy = tk.StringVar(value="XY  —")
        ttk.Label(actions, textvariable=self.xy).pack(side="left")
        self.field_recover = ttk.Button(actions, text="确认现场恢复", command=self.recover_field)
        self.field_recover.pack(side="right")
        self.preflight_button = ttk.Button(connection, text="开机检查", command=self.open_preflight)
        self.preflight_button.pack(fill="x", pady=(4, 0))
        ttk.Separator(inspector).pack(fill="x", pady=5)

        recipe = section("02", "测量配方")
        self.recipe_button = ttk.Button(recipe, text="加载现场配方", command=self.choose_recipe)
        self.recipe_button.pack(fill="x", pady=(4, 8))
        actions = row(recipe)
        self.edit_recipe_button = ttk.Button(actions, text="新建／编辑配方", command=self.edit_recipe)
        self.edit_recipe_button.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.save_recipe_button = ttk.Button(actions, text="保存现场配方", command=self.save_recipe)
        self.save_recipe_button.pack(side="right", fill="x", expand=True)
        self.recipe_info = tk.StringVar(value="现场配方未加载")
        ttk.Label(recipe, textvariable=self.recipe_info, wraplength=250, padding=(0, 8)).pack(anchor="w")
        ttk.Separator(inspector).pack(fill="x", pady=5)

        measurement = section("03", "采集测量")
        self.path_button = ttk.Button(measurement, text="编辑点位路径", command=self.edit_path)
        self.path_button.pack(fill="x", pady=(3, 4))
        self.import_button = ttk.Button(measurement, text="从 CSV 导入点位（可选）", command=self.choose_points)
        self.import_button.pack(fill="x", pady=(3, 9))
        self.field_batch = ttk.Button(measurement, text="开始批量测角", command=self.run_field_batch,
                                      style="Accent.TButton")
        self.field_batch.pack(fill="x", pady=4)
        self.field_af = ttk.Button(measurement, text="当前点 AF", command=self.run_field_af)
        self.field_af.pack(fill="x", pady=4)
        point_tools = ttk.Frame(measurement)
        ttk.Button(measurement, text="点位管理", command=lambda: (
            point_tools.pack_forget() if point_tools.winfo_manager() else point_tools.pack(fill="x")
        )).pack(anchor="w", pady=6)
        actions = row(point_tools)
        self.teach_button = ttk.Button(actions, text="示教当前 XY", command=self.teach)
        self.teach_button.pack(side="left")
        self.delete_button = ttk.Button(actions, text="删除选中点", command=self.delete_point)
        self.delete_button.pack(side="right")
        self.points = tk.Listbox(point_tools, height=3, exportselection=False)
        self.points.pack(fill="x")

        metric_panel, metrics = rounded_panel(main)
        metric_panel.pack(fill="x", pady=(0, 12))
        self.kpi = [tk.StringVar(value="—") for _ in range(4)]
        for index, (label, value) in enumerate(zip(("Rx", "Ry", "RMS", "PV"), self.kpi)):
            metrics.columnconfigure(index, weight=1, uniform="metric")
            tile = tk.Frame(metrics, bg=PAPER)
            tile.grid(row=0, column=index, sticky="ew", padx=6)
            tk.Label(tile, text=label, bg=PAPER, fg=MUTED,
                     font=("Segoe UI", 10)).pack(anchor="w")
            readings = tk.Frame(tile, bg=PAPER)
            readings.pack(anchor="w")
            tk.Label(readings, textvariable=value, bg=PAPER, fg="#315f89" if index < 2 else ink,
                     font=("Segoe UI", 19, "bold")).pack(side="left")
            tk.Label(readings, text="°" if index < 2 else "µm", bg=PAPER,
                     fg=MUTED).pack(side="left", anchor="s", padx=(3, 0), pady=(0, 5))

        plot_panel, plot_frame = rounded_panel(main)
        tk.Label(plot_frame, text="XY 点位", bg=PAPER, fg=ink, padx=4, pady=4,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        self.plot = tk.Canvas(plot_frame, background=PAPER, highlightthickness=0)
        self.plot.pack(fill="both", expand=True)
        self.plot.bind("<Configure>", self.draw)
        detail_panel, detail = rounded_panel(main)
        detail_panel.pack(side="bottom", fill="x")
        plot_panel.pack(fill="both", expand=True, pady=(0, 11))
        tk.Label(detail, text="逐点明细", bg=PAPER, fg=ink, padx=4, pady=4,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        table_body = ttk.Frame(detail, style="Paper.TFrame")
        table_body.pack(fill="both", expand=True)
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
        self.table.tag_configure("stripe", background="#f7f6f5")
        vertical = ttk.Scrollbar(table_body, orient="vertical", command=self.table.yview)
        horizontal = ttk.Scrollbar(table_body, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        horizontal.pack(side="bottom", fill="x")
        vertical.pack(side="right", fill="y")
        self.table.pack(side="left", fill="both", expand=True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_controls()
        root.after(40, self.poll)

    def clear_result(self):
        for value in self.kpi:
            value.set("—")

    def release_gateway(self):
        if self.workflow and isinstance(self.workflow.device, GatewayDevice):
            self.workflow.device.close()

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
            ("gateway", "Gateway 只读 XY"), ("stage", "载物台状态"),
            ("recipe", "测量配方"), ("license", "ZEN API 许可证"),
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
                        command=update_license).grid(row=8, column=2, sticky="w", pady=6)
        controls = ttk.Frame(panel)
        controls.grid(row=9, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(controls, text="蔡司官方排查文档", command=lambda: webbrowser.open(
            "https://github.com/zeiss-microscopy/OAD/blob/master/ZEN-API/README.md#troubleshooting"
        )).pack(side="left", padx=(0, 10))
        close = ttk.Button(controls, text="关闭", command=dialog.destroy)
        close.pack(side="right", padx=(8, 0))

        def run_check():
            env = {**os.environ, **self.gateway_overrides}
            updates = Queue()
            for key in ("address", "ca", "token", "gateway", "stage"):
                update_row(key, "… 检查中", "")
            recipe = self.field_recipe
            if recipe is None:
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
                    check_gateway(env, lambda *result: updates.put(result))
                except Exception:
                    for key in ("address", "ca", "token", "gateway", "stage"):
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

        self.pending = None
        self.field_verified = False
        self.clear_result()
        self.xy.set("XY  —")
        self.table.delete(*self.table.get_children())
        self.progress["value"] = 0
        self.plot.delete("all")
        self.release_gateway()
        self.workflow = Workflow(GatewayDevice(channel), self.data_root,
                                 simulated=False, validate=validate_recipe, fit=fit_field)
        self.show_field_heading()
        self.workflow.connect()
        self.refresh_controls()

    def draw(self, event):
        c = self.plot
        c.delete("all")
        points = DEMO_POINTS if self.workflow and self.workflow.simulated else (
            self.field_recipe.targets if self.field_recipe else ()
        )
        if not points:
            c.create_text(event.width / 2, event.height / 2, text="等待加载或示教 XY 点位", fill="#64748b")
            return
        xs, ys = zip(*[(x * 1e6, y * 1e6) for x, y in points])
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        left, top = 105, 22
        right, bottom = event.width - 56, event.height - 58
        if right <= left or bottom <= top:
            return
        xpad, ypad = (xmax - xmin or 100) * .1, (ymax - ymin or 100) * .1
        xmin, xmax, ymin, ymax = xmin-xpad, xmax+xpad, ymin-ypad, ymax+ypad
        ink = INK
        divisions = 6 if right - left < 520 else 12
        for index in range(divisions + 1):
            ratio = index / divisions
            x = left + ratio * (right-left)
            y = bottom - ratio * (bottom-top)
            c.create_line(x, top, x, bottom, fill="#eae9e7")
            c.create_line(left, y, right, y, fill="#eae9e7")
            c.create_text(x, bottom+16, text=f"{xmin + ratio*(xmax-xmin):,.0f}", fill=ink)
            c.create_text(left-10, y, anchor="e", text=f"{ymin + ratio*(ymax-ymin):,.0f}", fill=ink)
        c.create_line(left, top, left, bottom, right, bottom, fill=ink)
        c.create_text((left+right)/2, bottom+39, text="X (µm)", fill=ink)
        c.create_text(27, (top+bottom)/2, text="Y (µm)", angle=90, fill=ink)
        positions = [(left + (x-xmin)/(xmax-xmin)*(right-left),
                      bottom - (y-ymin)/(ymax-ymin)*(bottom-top)) for x, y in zip(xs, ys)]
        if len(positions) > 1:
            c.create_line(*[value for point in positions for value in point],
                          dash=(6, 4), width=2, fill="#9ab4ca")
        for index, (x, y) in enumerate(positions, 1):
            c.create_oval(x-6, y-6, x+6, y+6, fill=ACCENT, outline="")
            c.create_text(x+10, y-13, anchor="w", text=str(index), fill=ink,
                          font=("Segoe UI", 13, "bold"))

    def show_field_heading(self):
        name = self.field_recipe.name if self.field_recipe else "未加载配方"
        state = self.workflow.state
        self.heading.set(f"现场 · {state}   |   {name}   |   工程模式")

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
            self.plot.event_generate("<Configure>", width=self.plot.winfo_width(),
                                     height=self.plot.winfo_height())
        self.refresh_controls()

    def edit_path(self):
        if not self.field_recipe or (self.workflow and self.workflow.busy):
            return
        def apply(targets):
            self.apply_field_recipe(replace(self.field_recipe, targets=targets))
            self.plot.event_generate("<Configure>", width=self.plot.winfo_width(),
                                     height=self.plot.winfo_height())
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
        self.field_verified = False
        self.clear_result()
        self.progress.configure(maximum=1, value=0)
        workflow.start(recipe, path_confirmed=True, single_point=True)
        self.refresh_controls()

    def field_ready(self):
        workflow = self.workflow
        return bool(workflow and not workflow.simulated and not workflow.busy
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
        self.release_gateway()
        self.workflow = Workflow(SimulatedDevice(fault=fault), self.data_root,
                                 simulated=True, validate=validate_recipe, fit=fit_demo)
        recipe = Recipe("演示配方", "Demo AF", DEMO_POINTS, 1e-6, 3, 2, DEMO_CONVENTION)
        self.pending = (recipe, single_point)
        self.workflow.connect()
        self.heading.set("演示配方   |   工程模式")
        self.plot.event_generate("<Configure>", width=self.plot.winfo_width(),
                                 height=self.plot.winfo_height())
        self.refresh_controls()

    def recover(self):
        if not self.recovery_runs or (self.workflow and self.workflow.busy):
            return
        if not messagebox.askyesno("演示恢复", "模拟现场确认 XY 与 AF/Z 动作均已结束？"):
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
            self.release_gateway()
            self.workflow = None
            self.heading.set("未连接   |   工程模式")
            self.status.set("演示恢复已记录，可重新运行")
        self.refresh_controls()

    def recover_field(self):
        if not self.field_recovery_runs or (self.workflow and self.workflow.busy):
            return
        if not messagebox.askyesno(
            "独立确认 XY", "请按现场规程在设备端检查。是否已独立确认载物台 XY 动作结束？"
        ):
            return
        if not messagebox.askyesno(
            "独立确认 AF/Z",
            "请在 ZEN 或设备端独立确认软件 AF 与 Z 轴动作均已结束。是否已确认？",
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
        self.mode_badge.configure(text="演示数据" if simulated else "现场",
                                  bg="#f08316" if simulated else "#dce8f9",
                                  fg="white" if simulated else "#10244a")
        busy = bool(workflow and workflow.busy)
        locked = bool(self.recovery_runs or (
            workflow and workflow.simulated and workflow.state == "设备状态待确认"
        ))
        for button in (self.demo, self.fault, self.single):
            button.configure(state="disabled" if busy or locked else "normal")
        self.recover_button.configure(state="normal" if self.recovery_runs and not busy else "disabled")
        self.export_button.configure(
            state="normal" if workflow and workflow.result is not None and not busy else "disabled"
        )
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.connect_button.configure(state="disabled" if busy else "normal")
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

    def poll(self):
        workflow = self.workflow
        if workflow:
            while True:
                try:
                    kind, *data = workflow.events.get_nowait()
                except Empty:
                    break
                if kind == "state":
                    state, point, detail = data
                    if not workflow.simulated:
                        self.show_field_heading()
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
                    self.status.set(message)
                elif kind == "verified_xy":
                    self.verified_xy = data[0]
                elif kind == "xy":
                    x, y = data[0]
                    self.xy.set(f"XY  {x*1e6:.3f}, {y*1e6:.3f} µm")
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
                elif kind == "result":
                    r = data[0]
                    for value, formatted in zip(self.kpi, (
                        f"{r['rx_deg']:+.6f}", f"{r['ry_deg']:+.6f}",
                        f"{r['rms_m']*1e6:.3f}", f"{r['pv_m']*1e6:.3f}",
                    )):
                        value.set(formatted)
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
