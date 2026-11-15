"""Preview never authorizes motion. Runtime plots belong to the current run."""

from copy import deepcopy
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtWidgets import (
    QWidget,
    QComboBox,
    QLineEdit,
    QPushButton,
    QCheckBox,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QLabel,
    QFileDialog,
    QHeaderView,
    QScrollArea,
)
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from zen_steps import (
    migrate_program,
    validate_program,
    preview_program,
    capability_issues,
    flatten,
    CATALOG,
)
from zen_records import canonical_sha256
from ..widgets import Card, hbox, vbox, label
from .. import theme
from .device import _page


class MeasurementView(QWidget):
    def __init__(self):
        super().__init__()
        layout = vbox(self, margins=0)
        self.selector = QComboBox()
        layout.addWidget(self.selector)
        import matplotlib

        matplotlib.rcParams["font.sans-serif"] = [
            "Microsoft YaHei",
            "Noto Sans CJK SC",
            "Noto Sans CJK JP",
            "DejaVu Sans",
        ]
        self.metrics = label("Rx — µrad     Ry — µrad     RMS — µm", wrap=True)
        layout.addWidget(self.metrics)
        self.figure = Figure(figsize=(5, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumHeight(480)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(self.canvas)
        layout.addWidget(scroll, 1)
        self.planned = {}
        self.samples = {}
        self.selector.currentIndexChanged.connect(self.redraw)
        self.redraw()

    def configure(self, body, reset=False):
        old = self.selector.currentData()
        self.planned = {
            n["step_id"]: n
            for _, n in flatten(body.get("program_steps", []))
            if n["kind"] == "measure_angle"
        }
        if reset:
            self.samples = {}
            self.metrics.setText("Rx — µrad     Ry — µrad     RMS — µm")
        self.selector.blockSignals(True)
        self.selector.clear()
        for sid, n in self.planned.items():
            self.selector.addItem(n["label"] + " · " + sid, sid)
        self.selector.setCurrentIndex(max(0, self.selector.findData(old)))
        self.selector.blockSignals(False)
        self.redraw()

    def begin(self, data):
        self.samples[data["step_id"]] = []
        self.metrics.setText("正在采集本次测角点，拟合结果待完成")
        i = self.selector.findData(data["step_id"])
        if i >= 0:
            self.selector.setCurrentIndex(i)
        self.redraw()

    def append(self, data):
        self.samples.setdefault(data["step_id"], []).append(data)
        self.redraw()

    def redraw(self, *_):
        self.figure.clear()
        # Fixed boxes: the 3D axes ignore tight layout and used to print their
        # title over the XY plot's axis label on short windows.
        path = self.figure.add_axes((0.14, 0.60, 0.80, 0.33))
        scatter = self.figure.add_axes((0.10, 0.03, 0.80, 0.42), projection="3d")
        path.set_title("扫描路径 · XY", fontsize=11)
        path.set_xlabel("X · µm")
        path.set_ylabel("Y · µm")
        path.grid(alpha=0.2)
        sid = self.selector.currentData()
        points = []
        for row in self.planned.get(sid, {}).get("params", {}).get("points_um", []):
            try:
                points.append((float(row[0]), float(row[1])))
            except (ValueError, TypeError, IndexError):
                pass
        if points:
            xs, ys = zip(*points)
            path.plot(xs, ys, "o--", color="#94a3b8", markersize=4)
            for i, (x, y) in enumerate(points, 1):
                path.annotate(
                    str(i),
                    (x, y),
                    xytext=(4, 4),
                    textcoords="offset points",
                    fontsize=8,
                )
            path.set_aspect("equal", adjustable="datalim")
        samples = self.samples.get(sid, [])
        if samples:
            path.scatter(
                [p["x_um"] for p in samples],
                [p["y_um"] for p in samples],
                c="#2563eb",
                s=30,
                zorder=3,
            )
            scatter.scatter(
                [p["x_um"] for p in samples],
                [p["y_um"] for p in samples],
                [p["z_um"] for p in samples],
                c=[p["z_um"] for p in samples],
                cmap="viridis",
                s=26,
            )
        else:
            scatter.text2D(
                0.12,
                0.5,
                "尚无本次实测 Z 数据",
                transform=scatter.transAxes,
                color="#64748b",
            )
        scatter.set_title(f"实测 Z 散点 · {len(samples)} 点", fontsize=11)
        scatter.set_xlabel("X · µm")
        scatter.set_ylabel("Y · µm")
        scatter.set_zlabel("Z · µm")
        scatter.tick_params(labelsize=8)
        self.canvas.draw_idle()


class MarkView(QWidget):
    def __init__(self):
        super().__init__()
        layout = vbox(self, margins=0)
        self.selector = QComboBox()
        layout.addWidget(self.selector)
        self.frames = {}
        self.pictures = {}
        self.labels = {}
        body = QWidget()
        bl = hbox(body, margins=0)
        for layer, title in [("upper", "上 mark"), ("lower", "下 mark")]:
            panel = Card(title)
            pic = QLabel("本次尚未采集")
            pic.setAlignment(Qt.AlignCenter)
            pic.setMinimumSize(110, 180)
            panel.add(pic)
            status = label("等待采集", wrap=True)
            panel.add(status)
            bl.addWidget(panel)
            self.pictures[layer] = pic
            self.labels[layer] = status
        layout.addWidget(body, 1)
        self.selector.currentIndexChanged.connect(self.show_pair)

    def clear(self):
        self.frames = {}
        self.selector.clear()
        self.show_pair()

    def accept(self, data):
        key = data.get("context", "") + data["pair_id"]
        self.frames.setdefault(key, {})[data["layer"]] = data
        if self.selector.findData(key) < 0:
            self.selector.addItem(key, key)
        self.selector.setCurrentIndex(self.selector.findData(key))
        self.show_pair()

    def show_pair(self, *_):
        pair = self.frames.get(self.selector.currentData(), {})
        for layer in ("upper", "lower"):
            item = pair.get(layer)
            if not item:
                self.pictures[layer].clear()
                self.pictures[layer].setText("本次尚未采集")
                self.labels[layer].setText("等待采集")
                continue
            try:
                import tifffile, numpy as np

                data = tifffile.imread(item["path"])
                low, high = np.percentile(data, [1, 99])
                array = np.ascontiguousarray(
                    np.clip(
                        (data.astype(float) - low) / max(high - low, 1) * 255, 0, 255
                    ).astype("uint8")
                )
                if array.ndim != 2:
                    raise ValueError("预览仅支持单通道")
                q = QImage(
                    array.data,
                    array.shape[1],
                    array.shape[0],
                    array.strides[0],
                    QImage.Format_Grayscale8,
                ).copy()
                self.pictures[layer].setPixmap(
                    QPixmap.fromImage(q).scaled(
                        max(100, self.pictures[layer].width()),
                        max(100, self.pictures[layer].height()),
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )
                )
                self.labels[layer].setText(
                    f"已保存 · Z {item['z_um']:.3f} µm\n{item['experiment']}\n{Path(item['path']).name}"
                )
                self.pictures[layer].setToolTip(item["path"])
            except (OSError, ValueError) as e:
                self.pictures[layer].setText("预览不可用")
                self.labels[layer].setText(str(e))


class RunCenter(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self._selection = None
        self._checked = None
        self._scope = None
        self._from_here = False
        self._body = None
        self._display_hash = None
        self._recorded = False
        self._running_id = None
        self._tree_items = {}
        self._verdict_kind = "idle"
        lay, head = _page(self, "运行中心", "选择配方 → 核对 → 启动；结论显示在中间上方")
        for title, fn in [
            ("查看配方", lambda: app.navigate("recipes")),
            ("完整任务", self.clear_scope),
            ("离线演示", self.demo),
        ]:
            head.addWidget(QPushButton(title, clicked=fn))
        split = QSplitter()
        lay.addWidget(split, 1)
        left = QWidget()
        ll = vbox(left, margins=0)
        task = Card("本次任务")
        self.recipes = QComboBox()
        self.recipes.currentIndexChanged.connect(self.selection_changed)
        task.add(self.recipes)
        self.mode = QComboBox()
        self.mode.addItem("正式运行", "production")
        self.mode.addItem("工程试运行", "engineering")
        self.mode.addItem("离线演示", "simulation")
        self.mode.currentIndexChanged.connect(
            lambda _: self.invalidate(clear_confirmations=True)
        )
        task.add(self.mode)
        self.version_label = label("请选择配方", wrap=True)
        task.add(self.version_label)
        self.summary = label("", wrap=True)
        task.add(self.summary)
        self.output = QLineEdit(
            str(
                app.preferences.data.get(
                    "image_output", app.data_root.parent / "measurement-images"
                )
            )
        )
        self.output.textChanged.connect(self.invalidate)
        from PySide6.QtWidgets import QFormLayout

        identity = QWidget()
        form = QFormLayout(identity)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("样品 / 批次", app.sample_edit)
        form.addRow("操作员", app.operator_edit)
        task.add(identity)
        self.output.setToolTip("图像输出目录")
        output_row = QWidget()
        orow = hbox(output_row, margins=0)
        orow.addWidget(label("输出目录", role="muted"))
        orow.addWidget(self.output, 1)
        orow.addWidget(QPushButton("…", clicked=self.choose_output, toolTip="选择图像输出目录"))
        task.add(output_row)
        ll.addWidget(task)
        check = Card("执行前检查")
        self.path_confirm = QCheckBox("已核对装夹、运动路径及允许 Z 范围")
        self.load_confirm = QCheckBox("已验证实验不换物镜、不恢复未知位置")
        for c in (self.path_confirm, self.load_confirm):
            c.toggled.connect(self.refresh_enabled)
            check.add(c)
        self.status = label("请选择配方，查看完整步骤。", wrap=True)
        check.add(self.status)
        self._next_action_cb = None
        self.next_action = QPushButton()
        self.next_action.clicked.connect(
            lambda: self._next_action_cb() if self._next_action_cb else None
        )
        self.next_action.hide()
        check.add(self.next_action)
        self.check_button = QPushButton("执行前检查", clicked=self.preflight)
        self.start_button = QPushButton("启动任务", clicked=self.start)
        self.start_button.setObjectName("primary")
        check.add(self.check_button)
        check.add(self.start_button)
        ll.addWidget(check)
        ll.addStretch()
        # Short windows scroll the task column instead of squashing the
        # fields and pushing the start button off-screen.
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QScrollArea.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setWidget(left)
        left_scroll.setMinimumWidth(250)
        split.addWidget(left_scroll)
        center = Card("测量与采集")
        self.verdict = QLabel()
        self.verdict.setWordWrap(True)
        self.verdict.setTextFormat(Qt.RichText)
        center.add(self.verdict)
        self.tabs = QTabWidget()
        self.measure = MeasurementView()
        self.marks = MarkView()
        self.tabs.addTab(self.measure, "物料测角 · 路径 / Z 散点")
        self.tabs.addTab(self.marks, "上下 mark · 图像")
        center.add(self.tabs)
        self.current_action = label("当前动作：等待开始", wrap=True)
        center.add(self.current_action)
        split.addWidget(center)
        right = Card("完整执行计划")
        self.stop_button = QPushButton("停止后续步骤", clicked=app.stop)
        self.stop_button.setToolTip(
            "软件停止：不再下发后续步骤，当前设备动作会执行完。不是硬件急停。"
        )
        self.stop_button.setObjectName("danger")
        self.stop_button.setProperty("idle", True)
        right.add(self.stop_button)
        self.steps = QTreeWidget()
        self.steps.setHeaderLabels(["步骤 / 动作", "状态"])
        self.steps.setMinimumWidth(260)
        self.steps.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.steps.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        right.add(self.steps)
        self.result_label = label(self.IDLE_RESULT, wrap=True)
        right.add(self.result_label)
        split.addWidget(right)
        split.setSizes([290, 540, 380])
        app.sample_edit.textChanged.connect(
            lambda _: self.invalidate(clear_confirmations="path")
        )
        app.operator_edit.textChanged.connect(self.invalidate)
        self.reset_result()

    IDLE_RESULT = "任务：未开始\n对位分析：待 SOMA"
    VERDICT_COLORS = {
        "idle": (theme.INSET, theme.MUTED, theme.LINE),
        "run": (theme.ACCENT_SOFT, theme.ACCENT_INK, "#c5d9f5"),
        "ok": (theme.OK_SOFT, theme.OK, "#bfe3d0"),
        "warn": (theme.WARN_SOFT, theme.WARN, theme.WARN_LINE),
        "bad": (theme.BAD_SOFT, theme.BAD, theme.BAD_LINE),
    }

    def set_verdict(self, kind, title, detail="", simulated=False):
        """The one place an operator reads the outcome of the current run."""
        from html import escape

        self._verdict_kind = kind
        bg, ink, line = self.VERDICT_COLORS[kind]
        size = 14 if kind == "idle" else 20
        text = f'<div style="font-size:{size}px;font-weight:700;color:{ink}">{escape(title)}</div>'
        if simulated:
            text = (
                f'<div style="font-weight:700;color:{theme.WARN}">离线演示结果 · 非现场数据</div>'
                + text
            )
        if detail:
            text += f'<div style="color:{theme.INK}">{escape(detail).replace(chr(10), "<br>")}</div>'
        self.verdict.setText(text)
        self.verdict.setStyleSheet(
            f"background:{bg}; border:1px solid {line}; border-radius:8px; padding:8px 10px;"
        )

    def next_step(self):
        """First unmet start condition, as (text, done_flags)."""
        app = self.app
        done = [
            bool(app.workflow),
            bool(self._selection),
            bool(app.sample_edit.text().strip() and app.operator_edit.text().strip()),
            self.path_confirm.isChecked() and self.load_confirm.isChecked(),
        ]
        names = ["连接设备", "选择配方", "填写样品 / 操作员", "勾选两项核对"]
        hints = [
            "先到“设备与手动操作”连接设备，或点右上角“离线演示”。",
            "在左侧“本次任务”选择配方；还没有配方时到“配方管理”新建。",
            "在左侧“本次任务”填写样品 / 批次和操作员。",
            "在“执行前检查”勾选两项现场核对。",
        ]
        strip = " → ".join(
            ("✓ " if ok else f"{i} ") + name
            for i, (ok, name) in enumerate(zip(done, names), 1)
        ) + " → 启动任务"
        pending = [hint for ok, hint in zip(done, hints) if not ok]
        return strip, (pending[0] if pending else "条件已齐备，可以点击“启动任务”。")

    def reset_result(self, note=""):
        """Forget the previous run on screen; archived records are untouched."""
        self.result_label.setText(self.IDLE_RESULT)
        self.current_action.setText("当前动作：等待开始")
        self.current_action.setToolTip("")
        self._verdict_kind = "idle"
        self._idle_note = note
        self.refresh_guide()

    def refresh_guide(self):
        if self._verdict_kind != "idle" or not hasattr(self, "path_confirm"):
            return
        strip, hint = self.next_step()
        note = getattr(self, "_idle_note", "")
        self.set_verdict("idle", "下一步：" + hint, strip + ("\n" + note if note else ""))

    def refresh_identity(self):
        for edit in (self.app.sample_edit, self.app.operator_edit):
            edit.setStyleSheet(
                "" if edit.text().strip() else f"border:1px solid {theme.BAD};"
            )

    def library(self):
        return self.app.pages["recipes"].library

    def on_show(self):
        old = self.recipes.currentData()
        self.recipes.blockSignals(True)
        self.recipes.clear()
        for recipe in self.library().recipes():
            for v in reversed(recipe["revisions"]):
                if v["status"] == "archived":
                    continue
                self.recipes.addItem(
                    f"{recipe['name']} · Rev.{v['revision']:03d} · "
                    + ("正式版" if v["status"] == "released" else "草稿")
                    + (" · 演示" if v["body"].get("example") else ""),
                    (recipe["recipe_id"], v["revision"]),
                )
        i = next(
            (i for i in range(self.recipes.count()) if self.recipes.itemData(i) == old),
            0,
        )
        self.recipes.setCurrentIndex(i)
        self.recipes.blockSignals(False)
        value = self.recipes.currentData()
        version = self.library().get(*value) if value else None
        if value != self._selection or (
            version and version["content_sha256"] != self._display_hash
        ):
            self.selection_changed()

    def select_recipe(
        self, rid, rev, *, mode="engineering", step=None, from_here=False
    ):
        self.on_show()
        index = next(
            (
                i
                for i in range(self.recipes.count())
                if self.recipes.itemData(i) == (rid, rev)
            ),
            -1,
        )
        self.recipes.setCurrentIndex(index)
        self.mode.setCurrentIndex(self.mode.findData(mode))
        self._scope = step
        self._from_here = from_here
        self._checked = None
        self.preview()
        self.invalidate(clear_confirmations=True)

    def selection_changed(self, *_):
        self._selection = self.recipes.currentData()
        self._scope = None
        self._from_here = False
        self._checked = None
        self._body = None
        if self._selection:
            v = self.library().get(*self._selection)
            self._body = migrate_program(v["body"])
            self._display_hash = v["content_sha256"]
            if v["status"] == "draft" and self.mode.currentData() == "production":
                self.mode.blockSignals(True)
                self.mode.setCurrentIndex(self.mode.findData("engineering"))
                self.mode.blockSignals(False)
            self.version_label.setText(
                f"Rev.{v['revision']:03d} · "
                + ("正式版" if v["status"] == "released" else "草稿（可工程试运行）")
            )
            self.measure.configure(self._body, reset=True)
            self.marks.clear()
        else:
            self.measure.configure({"program_steps": []}, reset=True)
            self.marks.clear()
            self.version_label.setText("请选择配方")
        self.preview()
        self.invalidate(clear_confirmations=True)

    def preview(self):
        self.steps.clear()
        self._tree_items = {}
        if not self._body:
            return

        def add(nodes, parent=None):
            for i, n in enumerate(nodes, 1):
                item = QTreeWidgetItem([f"{i:02d} " + n["label"], "待检查"])
                item.setData(0, Qt.UserRole, n["step_id"])
                item.setToolTip(0, n["step_id"])
                (parent.addChild(item) if parent else self.steps.addTopLevelItem(item))
                self._tree_items[n["step_id"]] = item
                add(n.get("children", []), item)
                item.setExpanded(True)

        try:
            display, _ = self._snapshot_body()
        except ValueError:
            display = self._body
        add(display["program_steps"])
        self.summary.setText(
            f"{len(list(flatten(self._body['program_steps'])))} 个动作 · "
            + ("仅测试选中步骤/起点" if self._scope else "完整流程")
            + "\n设备绝对坐标 · XY/Z µm · Rx/Ry µrad"
        )
        self.measure.configure(display)

    def clear_scope(self):
        self._scope = None
        self._from_here = False
        self.preview()
        self.invalidate(clear_confirmations=True)

    def _snapshot_body(self):
        if not self._selection:
            raise ValueError("请先选择配方")
        v = self.library().get(*self._selection)
        if v["content_sha256"] != self._display_hash:
            self.selection_changed()
            v = self.library().get(*self._selection)
        if canonical_sha256(v["body"]) != v["content_sha256"]:
            raise ValueError("配方内容哈希不一致")
        b = deepcopy(self._body)
        if self._scope:
            nodes = b["program_steps"]
            flat = list(flatten(nodes))
            selected = next((n for _, n in flat if n["step_id"] == self._scope), None)
            if selected is None:
                raise ValueError("请选择可执行动作，不是公共参数")
            # Bootstrap only the preceding Load; it is explicit in preview and validated.
            previous = []
            for _, n in flat:
                if n["step_id"] == self._scope:
                    break
                if n["kind"] == "load_experiment":
                    previous = [n]
            if self._from_here:
                index = next(
                    (i for i, n in enumerate(nodes) if n["step_id"] == self._scope),
                    None,
                )
                if index is None:
                    raise ValueError("循环内动作不可作为中途起点，请选择整个循环")
                segment = nodes[index:]
            else:
                segment = [selected]
            needs_load = False
            for _, action in flatten(segment):
                if action["kind"] in ("load_experiment", "measure_angle"):
                    break
                if action["kind"] in (
                    "autofocus",
                    "capture_upper",
                    "capture_lower",
                    "zstack",
                ):
                    needs_load = True
                    break
            b["program_steps"] = (previous if needs_load else []) + segment
        return b, v

    def build_plan(self):
        b, v = self._snapshot_body()
        mode = self.mode.currentData()
        w = self.app.workflow
        if mode == "production" and (v["status"] != "released" or self._scope):
            raise ValueError("正式运行须选择已放行的完整配方")
        if mode == "production" and (
            v.get("validation", {}).get("state") != "qualified"
            or (v.get("checks") or {}).get("content_sha256") != v["content_sha256"]
        ):
            raise ValueError("正式版本的检查和放行证据不完整")
        if mode == "production" and "program_steps" not in v["body"]:
            raise ValueError("旧模板版本请复制为草稿并在新引擎重新验证")
        if w and not w.simulated and "program_steps" not in v["body"]:
            raise ValueError("旧模板须先在配方管理保存为新步骤版本，再工程试运行")
        if w and not w.simulated and b.get("example"):
            raise ValueError("演示配方不能用于现场")
        identity = {
            "recipe_id": self._selection[0],
            "revision": self._selection[1],
            "content_sha256": v["content_sha256"],
            "execution_mode": mode,
            "execution_scope": self._scope,
            "experiment_fingerprints": v.get("validation", {})
            .get("trial", {})
            .get("experiment_fingerprints", {}),
        }
        return validate_program(b, identity, scope=self._scope)

    def fingerprint(self):
        return canonical_sha256(
            {
                "body_hash": self.library().get(*self._selection)["content_sha256"],
                "mode": self.mode.currentData(),
                "scope": self._scope,
                "from_here": self._from_here,
                "sample": self.app.sample_edit.text(),
                "operator": self.app.operator_edit.text(),
                "output": self.output.text(),
            }
        )

    def notice(self, text, action=None, caption=None):
        self.status.setText(text)
        self._next_action_cb = action
        self.next_action.setVisible(action is not None)
        if caption:
            self.next_action.setText(caption)

    def edit_selected(self):
        page = self.app.pages["recipes"]
        if self._selection:
            if not page._resolve_dirty():
                return
            page.current_id, page.current_rev = self._selection
            page._body = None
            page._dirty = False
            page._selected_step_id = None
            page.refresh()
        self.app.navigate("recipes")

    def explain(self, error):
        text = str(error)
        if "样品" in text or "操作员" in text:
            control = (
                self.app.sample_edit
                if not self.app.sample_edit.text().strip()
                else self.app.operator_edit
            )
            self.notice(text, control.setFocus, "填写样品 / 操作员")
        elif "目录" in text:
            self.notice(text, self.choose_output, "选择图像目录")
        elif "已放行" in text or "正式版本" in text or "正式运行" in text:
            self.notice(
                text,
                lambda: self.mode.setCurrentIndex(self.mode.findData("engineering")),
                "切换工程试运行",
            )
        elif any(
            word in text for word in ("连接", "服务", "模拟设备", "接口", "不支持")
        ):
            self.notice(text, lambda: self.app.navigate("device"), "打开设备与手动操作")
        else:
            self.notice(text, self.edit_selected, "编辑当前配方参数")

    def invalidate(self, *_, clear_confirmations=False):
        from ..action_feedback import cancel_pending

        cancel_pending(self)
        self._checked = None
        if hasattr(self, "path_confirm"):
            if clear_confirmations:
                self.path_confirm.setChecked(False)
            if clear_confirmations is True:
                self.load_confirm.setChecked(False)
        if hasattr(self, "status"):
            self.notice(
                "可先勾选人工确认；点击“启动任务”会检查尚缺条件，不会盲目发送动作。"
            )
        self.refresh_enabled()

    def _ready_then(self, callback):
        w = self.app.workflow
        if self.app.device_ready():
            return True
        self.notice("正在只读检查设备就绪；不移动设备、不重新加载实验。")
        w.verify_ready()
        from ..action_feedback import defer_read

        def after():
            if self.app.device_ready():
                callback()
            else:
                self._auto_start_token = None
                self.notice(
                    "设备状态尚未通过检查。请查看事件日志；无 XY 的设备请在设备页选择仅 Z 模式。",
                    lambda: self.app.navigate("device"),
                    "检查设备连接与轴配置",
                )

        if not defer_read(self.app, self, after, self.fingerprint, self.notice):
            after()
        return False

    def preflight(self):
        try:
            w = self.app.workflow
            if not self._selection:
                self._auto_start_token = None
                self.notice(
                    "尚未选择配方。请先新建或选择配方。",
                    self.edit_selected,
                    "打开配方管理",
                )
                return
            if not w:
                self._auto_start_token = None
                self.notice(
                    "尚未连接设备。人工确认可先填写，执行检查需要连接。",
                    lambda: self.app.navigate("device"),
                    "连接设备 / 选择离线演示",
                )
                return
            from ..action_feedback import defer_read

            if defer_read(
                self.app, self, self.preflight, self.fingerprint, self.notice
            ):
                return
            p = self.build_plan()
            issues = capability_issues(p, w.device)
            if issues:
                for text in issues:
                    sid = text.split("：")[0].split("/")[-1]
                    if sid in self._tree_items:
                        self._tree_items[sid].setText(1, "不支持")
                raise ValueError("\n".join(issues))
            if (self.mode.currentData() == "simulation") != w.simulated:
                raise ValueError("离线演示须使用模拟设备；现场运行须连接现场设备")
            target = Path(self.output.text()).expanduser()
            if (
                not self.output.text().strip()
                or not target.is_absolute()
                or target.resolve().is_relative_to(self.app.data_root.resolve())
            ):
                raise ValueError("图像目录须为运行记录目录之外的绝对路径")
            if not self._ready_then(self.preflight):
                return
            token = self.fingerprint()
            self.notice("正在执行只读检查，请稍候。")

            def read(d):
                from math import isfinite

                z = d.read_z()
                xy = d.read_xy() if w.xy_enabled else None
                if not isfinite(z) or (
                    xy is not None and not all(isfinite(v) for v in xy)
                ):
                    raise ValueError("位置无效")
                if xy is not None:
                    from zen_hardware import Motion

                    if d.motion_state() is not Motion.IDLE:
                        raise ValueError("XY 未空闲")
                if (
                    p.common.get("z_min_um") is not None
                    and not p.common["z_min_um"] <= z * 1e6 <= p.common["z_max_um"]
                ):
                    raise ValueError("当前 Z 超出配方范围")
                return {
                    "token": token,
                    "device": id(d),
                    "generation": getattr(w, "action_generation", 0),
                    "xy": xy,
                    "z_m": z,
                    "optics": d.read_optics(),
                }

            w.read_only("program_preflight", read)
        except (ValueError, RuntimeError, TypeError) as e:
            self._auto_start_token = None
            self.explain(e)

    def on_rpc_result(self, tag, value):
        if (
            tag == "program_preflight"
            and self._selection is not None
            and self.app.workflow is not None
            and value["token"] == self.fingerprint()
            and value["device"] == id(self.app.workflow.device)
        ):
            self._checked = value
            self.notice("设备检查通过；完成两项人工确认后即可启动。")
            self.refresh_enabled()
            if getattr(self, "_auto_start_token", None) == value["token"]:
                from PySide6.QtWidgets import QApplication

                self._auto_start_token = None
                if QApplication.activeModalWidget() is not None:
                    self.notice(
                        "正在编辑对话框参数，已取消自动启动；编辑完成后请重新点击。"
                    )
                    return
                if getattr(self, "_auto_start_visible", False) and not self.isVisible():
                    self.notice("已离开运行中心，未启动任务。")
                    return
                self.start()

    def on_rpc_error(self, tag, error):
        if tag == "program_preflight":
            self._checked = None
            self._auto_start_token = None
            self.explain(error)

    def start(self):
        try:
            w = self.app.workflow
            if not self._selection:
                self.notice("请先选择或新建配方。", self.edit_selected, "打开配方管理")
                return
            p = self.build_plan()
            missing = []
            if not w:
                missing.append("设备尚未连接")
            if not self.app.sample_edit.text().strip():
                missing.append("填写样品 / 批次编号")
            if not self.app.operator_edit.text().strip():
                missing.append("填写操作员")
            if not self.path_confirm.isChecked():
                missing.append("人工核对装夹、运动路径及允许 Z 范围")
            if not self.load_confirm.isChecked():
                missing.append("人工核对实验不会换物镜或恢复未知位置")
            if missing:
                if not w:
                    action = lambda: self.app.navigate("device")
                    caption = "连接设备 / 选择离线演示"
                elif not self.app.sample_edit.text().strip():
                    action = self.app.sample_edit.setFocus
                    caption = "填写样品 / 批次"
                elif not self.app.operator_edit.text().strip():
                    action = self.app.operator_edit.setFocus
                    caption = "填写操作员"
                else:
                    target = (
                        self.path_confirm
                        if not self.path_confirm.isChecked()
                        else self.load_confirm
                    )
                    action = target.setFocus
                    caption = "定位待确认项"
                self.notice(
                    "启动条件尚未齐备："
                    + chr(10)
                    + "• "
                    + (chr(10) + "• ").join(missing),
                    action,
                    caption,
                )
                return
            from ..action_feedback import defer_read

            if defer_read(self.app, self, self.start, self.fingerprint, self.notice):
                return
            c = self._checked
            if (
                not c
                or c["token"] != self.fingerprint()
                or c["device"] != id(w.device)
                or c.get("generation") != getattr(w, "action_generation", 0)
                or not self.app.device_ready()
            ):
                self._auto_start_token = self.fingerprint()
                self._auto_start_visible = self.isVisible()
                self.preflight()
                return
            self.measure.configure(self._body, reset=True)
            self.marks.clear()
            self.preview()
            self.result_label.setText("任务：执行中\n角度：测量中\n对位分析：待 SOMA")
            self.current_action.setText("当前动作：正在启动")
            self.set_verdict(
                "run",
                "任务执行中",
                "本轮结论尚未产生；上一轮结果已归档到“结果与追溯”。",
                simulated=bool(w.simulated),
            )
            self._recorded = False
            self._running_id = p.recipe_identity
            w.start_program(
                p,
                self.output.text(),
                session={
                    "sample": self.app.sample_edit.text(),
                    "operator": self.app.operator_edit.text(),
                },
                preflight=c,
                confirmed=True,
            )
            self._checked = None
            self.notice("任务已启动；参数与本次检查已冻结，正在按执行计划运行。")
            self.persist()
        except (ValueError, RuntimeError) as e:
            self._auto_start_token = None
            self.explain(e)

    def on_workflow_event(self, kind, data, w):
        if kind == "measurement_begin":
            self.measure.begin(data[0])
            self.tabs.setCurrentIndex(0)
        elif kind == "measurement_point":
            self.measure.append(data[0])
        elif kind == "mark_image":
            self.marks.accept(data[0])
            self.tabs.setCurrentIndex(1)
        elif kind == "state" and data[0] == "自动任务":
            self.current_action.setText("当前动作：" + data[2])
        elif kind == "program_progress":
            report = data[0]
            result = report.get("result")
            if result:
                self.measure.metrics.setText(
                    f"Rx {result['rx_urad']:+.3f} µrad    Ry {result['ry_urad']:+.3f} µrad    RMS {result['rms_m']*1e6:.3f} µm"
                )
            names = {
                "running": "执行中",
                "complete": "完成",
                "failed": "失败",
                "stopped": "已停止",
            }
            for step in report["steps"]:
                item = self._tree_items.get(step["step_id"])
                if item:
                    item.setText(1, names.get(step["state"], step["state"]))
                    item.setToolTip(1, step.get("error", ""))
                    self.steps.setCurrentItem(item)
            if report["state"] != "running":
                rejected = report["disposition"] == "MEASUREMENT_REJECTED"
                self.result_label.setText(
                    "任务："
                    + names.get(report["state"], report["state"])
                    + "\n角度："
                    + (
                        "不合格"
                        if rejected
                        else "已测量" if result else "未启用 / 未完成"
                    )
                    + "\n对位分析：待 SOMA"
                )
                numbers = (
                    f"Rx {result['rx_urad']:+.3f} µrad    Ry {result['ry_urad']:+.3f} µrad    RMS {result['rms_m']*1e6:.3f} µm"
                    if result
                    else ""
                )
                message = report.get("error", {}).get("message", "")
                if rejected:
                    kind, title = "bad", "角度不合格"
                elif report["state"] == "complete":
                    kind, title = "ok", "任务完成" + (" · 角度已测量" if result else "")
                elif report["state"] == "stopped":
                    kind, title = "warn", "任务已停止 · 结果不完整"
                else:
                    kind, title = "bad", "任务失败"
                detail = "\n".join(
                    part
                    for part in (
                        numbers,
                        message,
                        "对位分析待 SOMA。" if kind == "ok" else "",
                    )
                    if part
                )
                self.set_verdict(kind, title, detail, simulated=bool(report["simulated"]))
                self.status.setText(
                    report.get("error", {}).get("message", "步骤流程完成，结果已归档。")
                )
                self.current_action.setText("记录已归档，可在“结果与追溯”查看。")
                self.current_action.setToolTip(str(w.sequence_run_path))
                identity = report["recipe_identity"]
                if (
                    report["state"] == "complete"
                    and not report["simulated"]
                    and identity["execution_mode"] == "engineering"
                    and not identity.get("execution_scope")
                    and not self._recorded
                ):
                    try:
                        self.library().record_trial(
                            identity["recipe_id"],
                            identity["revision"],
                            w.sequence_run_path,
                            by=self.app.operator_edit.text(),
                        )
                        self._recorded = True
                    except (ValueError, OSError) as e:
                        self.status.setText("验证证据未关联：" + str(e))

    def on_device_changed(self, w):
        # Results drawn from another device (or the demo) must not sit next to
        # the new connection state; the archive keeps them.
        shown = self._verdict_kind != "idle"
        self.measure.configure(self._body or {"program_steps": []}, reset=True)
        self.marks.clear()
        self.preview()
        self.reset_result(
            "设备连接已变化，上一轮结果已从本页清除；可在“结果与追溯”查看。"
            if shown
            else ""
        )
        self.invalidate(clear_confirmations=True)

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "图像目录")
        if path:
            self.output.setText(path)

    def persist(self):
        self.app.preferences.update(image_output=self.output.text())

    def load_demo_defaults(self):
        pass

    def demo(self):
        if self.app.action_busy():
            self.notice("当前任务执行中，请先等待结束或停止。")
            return
        p = self.app.pages["recipes"]
        old = p.current_id
        p.seed_demo()
        if old == p.current_id:
            return
        self.app.connect_demo("sequence")
        self.select_recipe(p.current_id, p.current_rev, mode="simulation")
        self.app.navigate("sequence")

    def refresh_enabled(self):
        if not hasattr(self, "start_button"):
            return
        busy = self.app.action_busy()
        for control in (self.recipes, self.mode, self.output):
            control.setEnabled(not busy)
            control.setToolTip(
                "设备任务执行中，当前运行已冻结；结束后可切换。" if busy else ""
            )
        self.check_button.setEnabled(True)
        self.path_confirm.setEnabled(True)
        self.load_confirm.setEnabled(True)
        self.start_button.setEnabled(True)
        name = "" if busy else "primary"
        if self.start_button.objectName() != name:
            self.start_button.setText("任务运行中…" if busy else "启动任务")
            self.start_button.setObjectName(name)
            self.start_button.style().unpolish(self.start_button)
            self.start_button.style().polish(self.start_button)
        self.start_button.setToolTip(
            "任务正在运行；需要中止请点“停止后续步骤”。"
            if busy
            else "点击后列出尚缺条件；条件齐备时自动做只读检查，再启动。"
        )
        self.refresh_identity()
        self.refresh_guide()
        w = self.app.workflow
        self.stop_button.setEnabled(
            bool(w and w.busy)
            or getattr(self, "_pending_click", None) is not None
            or getattr(self, "_auto_start_token", None) is not None
        )
        idle = not self.stop_button.isEnabled()
        if self.stop_button.property("idle") != idle:
            self.stop_button.setProperty("idle", idle)
            self.stop_button.style().unpolish(self.stop_button)
            self.stop_button.style().polish(self.stop_button)
