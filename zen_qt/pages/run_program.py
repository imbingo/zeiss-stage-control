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
        self.canvas.setMinimumHeight(380)
        layout.addWidget(self.canvas, 1)
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
        path = self.figure.add_subplot(211)
        scatter = self.figure.add_subplot(212, projection="3d")
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
        self.figure.subplots_adjust(
            left=0.13, right=0.93, top=0.93, bottom=0.10, hspace=0.38
        )
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
        lay, head = _page(self, "运行中心", "流程始终可预览；启动条件单独检查")
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
        self.mode.addItem("离线模拟", "simulation")
        self.mode.currentIndexChanged.connect(self.invalidate)
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
        task.add(label("图像输出目录", role="muted"))
        task.add(self.output)
        task.add(QPushButton("选择目录…", clicked=self.choose_output))
        ll.addWidget(task)
        check = Card("执行前检查")
        self.path_confirm = QCheckBox("已核对装夹、运动路径及允许 Z 范围")
        self.load_confirm = QCheckBox("已验证实验不换物镜、不恢复未知位置")
        for c in (self.path_confirm, self.load_confirm):
            c.toggled.connect(self.refresh_enabled)
            check.add(c)
        self.status = label("请选择配方，查看完整步骤。", wrap=True)
        check.add(self.status)
        self.check_button = QPushButton("执行前检查", clicked=self.preflight)
        self.start_button = QPushButton("启动任务", clicked=self.start)
        self.start_button.setObjectName("primary")
        check.add(self.check_button)
        check.add(self.start_button)
        ll.addWidget(check)
        ll.addStretch()
        split.addWidget(left)
        center = Card("测量与采集")
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
        self.steps = QTreeWidget()
        self.steps.setHeaderLabels(["步骤 / 动作", "状态"])
        self.steps.setMinimumWidth(260)
        self.steps.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.steps.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        right.add(self.steps)
        self.result_label = label("任务：未开始\n对位分析：待 SOMA", wrap=True)
        right.add(self.result_label)
        self.stop_button = QPushButton("停止后续步骤", clicked=app.stop)
        right.add(self.stop_button)
        split.addWidget(right)
        split.setSizes([290, 540, 380])
        app.sample_edit.textChanged.connect(self.invalidate)
        app.operator_edit.textChanged.connect(self.invalidate)

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
            self.version_label.setText(
                f"Rev.{v['revision']:03d} · "
                + ("正式版" if v["status"] == "released" else "草稿（可工程试运行）")
            )
            self.measure.configure(self._body, reset=True)
            self.marks.clear()
        self.preview()
        self.invalidate()

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
        self.invalidate()

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
            raise ValueError("旧模板须先在配方管理保存为新步骤版本，再现场试运行")
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

    def invalidate(self, *_):
        self._checked = None
        if hasattr(self, "path_confirm"):
            self.path_confirm.setChecked(False)
            self.load_confirm.setChecked(False)
        if hasattr(self, "status"):
            self.status.setText("流程已显示；请完成参数检查和设备检查。")
        self.refresh_enabled()

    def preflight(self):
        try:
            w = self.app.workflow
            if not w or w.busy or not self.app.device_ready():
                raise ValueError("设备未连接或存在待恢复状态；仍可预览流程")
            p = self.build_plan()
            issues = capability_issues(p, w.device)
            if issues:
                for text in issues:
                    sid = text.split("：")[0].split("/")[-1]
                    if sid in self._tree_items:
                        self._tree_items[sid].setText(1, "不支持")
                raise ValueError("\n".join(issues))
            if (self.mode.currentData() == "simulation") != w.simulated:
                raise ValueError("离线模拟须使用模拟设备；现场运行须连接真实设备")
            if (
                not self.app.sample_edit.text().strip()
                or not self.app.operator_edit.text().strip()
            ):
                raise ValueError("请填写样品编号和工程师/操作员")
            target = Path(self.output.text()).expanduser()
            if (
                not self.output.text().strip()
                or not target.is_absolute()
                or target.resolve().is_relative_to(self.app.data_root.resolve())
            ):
                raise ValueError("图像目录须为运行记录目录之外的绝对路径")
            token = self.fingerprint()

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
                    "xy": xy,
                    "z_m": z,
                    "optics": d.read_optics(),
                }

            w.read_only("program_preflight", read)
        except (ValueError, RuntimeError, TypeError) as e:
            self.status.setText(str(e))

    def on_rpc_result(self, tag, value):
        if (
            tag == "program_preflight"
            and value["token"] == self.fingerprint()
            and value["device"] == id(self.app.workflow.device)
        ):
            self._checked = value
            self.status.setText("设备检查通过；请核对路线与实验行为后明确启动。")
            self.refresh_enabled()

    def on_rpc_error(self, tag, error):
        if tag == "program_preflight":
            self._checked = None
            self.status.setText(str(error))

    def start(self):
        try:
            w = self.app.workflow
            c = self._checked
            if not c or c["token"] != self.fingerprint() or c["device"] != id(w.device):
                raise ValueError("请重新检查")
            if not self.path_confirm.isChecked() or not self.load_confirm.isChecked():
                raise ValueError("请确认装夹、路径及实验")
            p = self.build_plan()
            self.measure.configure(self._body, reset=True)
            self.marks.clear()
            self.preview()
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
            self.path_confirm.setChecked(False)
            self.load_confirm.setChecked(False)
            self.persist()
        except (ValueError, RuntimeError) as e:
            self.status.setText(str(e))

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
                self.result_label.setText(
                    "任务："
                    + names.get(report["state"], report["state"])
                    + "\n角度："
                    + (
                        "不合格"
                        if report["disposition"] == "MEASUREMENT_REJECTED"
                        else "已测量" if report.get("result") else "未启用 / 未完成"
                    )
                    + "\n对位分析：待 SOMA"
                )
                self.status.setText(
                    report.get("error", {}).get("message", "步骤流程完成，结果已归档。")
                )
                self.current_action.setText("记录：" + str(w.sequence_run_path))
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
        self.invalidate()

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "图像目录")
        if path:
            self.output.setText(path)

    def persist(self):
        self.app.preferences.update(image_output=self.output.text())

    def load_demo_defaults(self):
        pass

    def demo(self):
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
        w = self.app.workflow
        busy = bool(w and w.busy)
        ready = bool(w and not busy and self.app.device_ready())
        self.recipes.setEnabled(not busy)
        self.mode.setEnabled(not busy)
        self.output.setEnabled(not busy)
        self.check_button.setEnabled(ready)
        self.path_confirm.setEnabled(ready)
        self.load_confirm.setEnabled(ready)
        self.stop_button.setEnabled(busy)
        self.start_button.setEnabled(
            ready
            and self._checked is not None
            and self.path_confirm.isChecked()
            and self.load_confirm.isChecked()
        )
        self.start_button.setToolTip("检查通过且已确认本次路线与实验后可启动")
