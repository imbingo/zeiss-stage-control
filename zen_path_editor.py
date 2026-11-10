"""Offline editing of an ordered XY measurement path, in micrometres."""

from math import isfinite
import tkinter as tk
from tkinter import ttk


def parse_xy(x, y):
    try:
        point = float(x) * 1e-6, float(y) * 1e-6
    except (ValueError, TypeError, OverflowError):
        raise ValueError("X、Y 请填写数字，单位为 µm") from None
    if not all(map(isfinite, point)):
        raise ValueError("X、Y 必须是有限数字")
    return point


class PathEditor(tk.Toplevel):
    def __init__(self, parent, targets, on_apply):
        super().__init__(parent)
        self.title("编辑点位路径")
        self.geometry("900x570")
        self.minsize(760, 470)
        self.transient(parent)
        self.targets = list(targets)
        self.on_apply = on_apply
        self.selected = None
        self.x = tk.StringVar()
        self.y = tk.StringVar()
        self.message = tk.StringVar()
        shell = ttk.Frame(self, padding=16)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="按顺序编辑测量路径", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(shell, text="输入载物台绝对坐标（µm）；此窗口仅编辑点位，不驱动设备。双击点位可修改。").pack(anchor="w", pady=(6, 12))
        body = ttk.Frame(shell)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 14))
        self.table = ttk.Treeview(left, columns=("index", "x", "y"), show="headings", selectmode="browse", height=10)
        for key, label, width in (("index", "顺序", 55), ("x", "X (µm)", 125), ("y", "Y (µm)", 125)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, anchor="center")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self.select)
        self.table.bind("<Double-1>", lambda event: self.x_entry.focus_set())
        self.preview = tk.Canvas(body, background="white", highlightthickness=1, highlightbackground="#deddda", width=360)
        self.preview.pack(side="right", fill="both", expand=True)
        self.preview.bind("<Configure>", lambda event: self.draw())
        entry = ttk.Frame(shell)
        entry.pack(fill="x", pady=(12, 6))
        ttk.Label(entry, text="X (µm)").pack(side="left")
        self.x_entry = ttk.Entry(entry, textvariable=self.x, width=16)
        self.x_entry.pack(side="left", padx=(6, 14))
        ttk.Label(entry, text="Y (µm)").pack(side="left")
        ttk.Entry(entry, textvariable=self.y, width=16).pack(side="left", padx=6)
        self.add_button = ttk.Button(entry, text="追加点位", command=self.add)
        self.add_button.pack(side="left", padx=6)
        self.update_button = ttk.Button(entry, text="更新选中点", command=self.update_point)
        self.update_button.pack(side="left")
        tools = ttk.Frame(shell)
        tools.pack(fill="x", pady=6)
        self.up_button = ttk.Button(tools, text="上移", command=lambda: self.move(-1))
        self.up_button.pack(side="left")
        self.down_button = ttk.Button(tools, text="下移", command=lambda: self.move(1))
        self.down_button.pack(side="left", padx=6)
        self.delete_button = ttk.Button(tools, text="删除选中点", command=self.delete)
        self.delete_button.pack(side="left")
        ttk.Label(shell, textvariable=self.message, wraplength=820).pack(anchor="w", pady=6)
        footer = ttk.Frame(shell)
        footer.pack(fill="x")
        ttk.Button(footer, text="应用路径", style="Accent.TButton", command=self.apply).pack(side="right")
        ttk.Button(footer, text="取消", command=self.destroy).pack(side="right", padx=8)
        self.refresh()
        self.grab_set()

    def refresh(self, selected=None):
        self.selected = selected
        self.table.delete(*self.table.get_children())
        for index, (x, y) in enumerate(self.targets):
            self.table.insert("", "end", iid=str(index), values=(index + 1, f"{x*1e6:.12g}", f"{y*1e6:.12g}"))
        if selected is not None:
            self.table.selection_set(str(selected))
            self.table.see(str(selected))
        self.message.set(f"共 {len(self.targets)} 个点，按列表从上到下采集。批量测角至少需要 3 个非共线点；应用修改后须重新验证当前点 AF。")
        self.controls()
        self.draw()

    def controls(self):
        index = self.selected
        for button, enabled in ((self.update_button, index is not None),
                                (self.delete_button, index is not None),
                                (self.up_button, index is not None and index > 0),
                                (self.down_button, index is not None and index < len(self.targets)-1)):
            button.configure(state="normal" if enabled else "disabled")

    def select(self, event=None):
        selection = self.table.selection()
        self.selected = int(selection[0]) if selection else None
        if self.selected is not None:
            x, y = self.targets[self.selected]
            self.x.set(f"{x*1e6:.12g}")
            self.y.set(f"{y*1e6:.12g}")
        self.controls()
        self.draw()

    def value(self):
        try:
            return parse_xy(self.x.get(), self.y.get())
        except ValueError as error:
            self.message.set(str(error))
            return None

    def add(self):
        point = self.value()
        if point is not None:
            self.targets.append(point)
            self.refresh(len(self.targets)-1)

    def update_point(self):
        if self.selected is None:
            return
        point = self.value()
        if point is not None:
            self.targets[self.selected] = point
            self.refresh(self.selected)

    def move(self, delta):
        index = self.selected
        if index is None or not 0 <= index + delta < len(self.targets):
            return
        self.targets[index], self.targets[index+delta] = self.targets[index+delta], self.targets[index]
        self.refresh(index+delta)

    def delete(self):
        if self.selected is not None:
            index = self.selected
            self.targets.pop(index)
            self.refresh(min(index, len(self.targets)-1) if self.targets else None)

    def apply(self):
        self.on_apply(tuple(self.targets))
        self.destroy()

    def draw(self):
        canvas = self.preview
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if not self.targets:
            canvas.create_text(width/2, height/2, text="添加点位后显示路径", fill="#626873")
            return
        xs, ys = zip(*self.targets)
        span = max(max(xs)-min(xs), max(ys)-min(ys), 1e-9)
        scale = max(1, min(width, height)-90)/span
        cx, cy = (min(xs)+max(xs))/2, (min(ys)+max(ys))/2
        positions = [(width/2+(x-cx)*scale, height/2-(y-cy)*scale) for x, y in self.targets]
        for a, b in zip(positions, positions[1:]):
            canvas.create_line(*a, *b, fill="#4779a6", width=2, arrow="last")
        for index, (x, y) in enumerate(positions):
            canvas.create_oval(x-5, y-5, x+5, y+5, fill="#e88935" if index == self.selected else "#4779a6", outline="")
            canvas.create_text(x+10, y-12, text=str(index+1), anchor="w")
        canvas.create_text(10, height-16, anchor="w", text="X →    Y ↑   ·   数字为采集顺序", fill="#626873")
