"""Production run centre: selected recipe revision is the only process input."""

from pathlib import Path
from dataclasses import replace
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QWidget,
    QComboBox,
    QPushButton,
    QLineEdit,
    QCheckBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QFileDialog,
    QMessageBox,
    QHeaderView,
)
from zen_recipe_compile import compile_revision
from zen_sequence import validate_capabilities
from zen_records import canonical_sha256
from ..widgets import Card, hbox, vbox, label
from .device import _page


class LayoutMap(QWidget):
    def __init__(self):
        super().__init__()
        self.plan = None
        self.setMinimumSize(300, 280)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#ffffff"))
        r = self.rect().adjusted(45, 25, -25, -40)
        p.setPen(QColor("#e5ebf2"))
        for i in range(7):
            x = r.left() + r.width() * i / 6
            y = r.top() + r.height() * i / 6
            p.drawLine(int(x), r.top(), int(x), r.bottom())
            p.drawLine(r.left(), int(y), r.right(), int(y))
        p.setPen(QColor("#64748b"))
        p.drawText(10, 18, "Y · µm")
        p.drawText(r.right() - 45, self.height() - 8, "X · µm")
        if not self.plan:
            p.drawText(r, Qt.AlignCenter, "选择配方后显示测量区域与转场路径")
            return
        a = list(self.plan.tilt.targets) if self.plan.measure_enabled else []
        b = [(s.x_m, s.y_m) for s in self.plan.stations]
        points = a + b
        if not points:
            return
        xs = [v[0] for v in points]
        ys = [v[1] for v in points]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        dx = max(xmax - xmin, 100e-6)
        dy = max(ymax - ymin, 100e-6)
        scale = min((r.width() - 60) / dx, (r.height() - 70) / dy)
        ox = r.left() + (r.width() - dx * scale) / 2
        oy = r.top() + (r.height() + dy * scale) / 2

        def pos(v):
            return QPointF(ox + (v[0] - xmin) * scale, oy - (v[1] - ymin) * scale)

        for group, title, color in [
            (a, "A 区 · 测角", "#2563eb"),
            (b, "B 区 · 孔位", "#099c8c"),
        ]:
            if not group:
                continue
            coords = [pos(v) for v in group]
            bounds = QRectF(
                min(v.x() for v in coords) - 15,
                min(v.y() for v in coords) - 15,
                max(max(v.x() for v in coords) - min(v.x() for v in coords) + 30, 35),
                max(max(v.y() for v in coords) - min(v.y() for v in coords) + 30, 35),
            )
            fill = QColor(color)
            fill.setAlpha(18)
            p.setBrush(fill)
            p.setPen(QPen(QColor(color), 1))
            p.drawRoundedRect(bounds, 5, 5)
            p.drawText(bounds.topLeft() + QPointF(0, -8), title)
        p.setPen(QPen(QColor("#ed982e"), 2, Qt.DashLine))
        for first, second in zip(points, points[1:]):
            p.drawLine(pos(first), pos(second))
        for i, v in enumerate(points):
            color = QColor("#2563eb" if i < len(a) else "#099c8c")
            p.setPen(color)
            p.setBrush(color)
            pt = pos(v)
            p.drawEllipse(pt, 5, 5)
            name = f"A{i+1}" if i < len(a) else self.plan.stations[i - len(a)].name
            p.drawText(pt + QPointF(8, -8), name)
        p.setPen(QColor("#64748b"))
        p.drawText(r.left(), self.height() - 8, f"{xmin*1e6:.0f}")
        p.drawText(r.right() - 90, self.height() - 8, f"{xmax*1e6:.0f}")


class RunCenter(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self._checked = None
        self._plan = None
        self._selection = None
        self._scope = None
        self._running_identity = None
        self._trial_recorded = False
        self._presented = []
        self._last_report = None
        self._display_hash = None
        lay, head = _page(
            self, "运行中心", "选择配方版本 → 确认样品与路径 → 检查并执行"
        )
        head.addWidget(QPushButton("编辑配方", clicked=lambda: app.navigate("recipes")))
        head.addWidget(QPushButton("完整任务", clicked=self.clear_scope))
        head.addWidget(QPushButton("离线演示", clicked=self.demo))
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
        self.mode.addItem("现场工程试运行", "engineering")
        self.mode.addItem("离线模拟", "simulation")
        self.mode.currentIndexChanged.connect(self.selection_changed)
        task.add(self.mode)
        self.version_label = label("请选择配方", wrap=True)
        task.add(self.version_label)
        self.summary = label("参数来自所选配方的独立快照。", wrap=True)
        task.add(self.summary)
        self.output = QLineEdit(
            str(
                app.preferences.data.get(
                    "image_output", app.data_root.parent / "measurement-images"
                )
            )
        )
        task.add(label("图像输出目录", role="muted"))
        task.add(self.output)
        self.output.textChanged.connect(self.invalidate)
        task.add(QPushButton("选择目录…", clicked=self.choose_output))
        ll.addWidget(task)
        check = Card("执行前检查")
        self.path_confirm = QCheckBox("已确认装夹基准、全部路径及安全 Z")
        self.path_confirm.setToolTip("本版使用设备绝对坐标，不自动对准样品")
        self.load_confirm = QCheckBox("已验证实验行为与上下层焦面")
        for c in [self.path_confirm, self.load_confirm]:
            c.toggled.connect(self.refresh_enabled)
            check.add(c)
        self.status = label("请选择已放行版本，或切换工程模式验证草稿。", wrap=True)
        check.add(self.status)
        self.check_button = QPushButton("执行前检查", clicked=self.preflight)
        self.start_button = QPushButton("启动任务", clicked=self.start)
        self.start_button.setObjectName("primary")
        check.add(self.check_button)
        check.add(self.start_button)
        ll.addWidget(check)
        ll.addStretch()
        split.addWidget(left)
        middle = Card("测量布局与转场路径")
        self.map = LayoutMap()
        middle.add(self.map)
        middle.add(
            label(
                "蓝色：测角点  ·  绿色：孔位/转场点\n虚线按执行顺序连接；所有 XY 转场前先到已验证安全 Z。",
                wrap=True,
                role="muted",
            )
        )
        self.route = label("设备绝对坐标 · 装夹基准须人工核对", wrap=True)
        middle.add(self.route)
        middle.add(
            label(
                "同一孔位：上层 → 下层，仅移动 Z。\n图片保存完成后，由 SOMA 分析对位。",
                wrap=True,
            )
        )
        split.addWidget(middle)
        right = Card("执行计划")
        self.steps = QTableWidget(0, 2)
        self.steps.setHorizontalHeaderLabels(["步骤 / 子动作", "状态"])
        self.steps.horizontalHeader().setStretchLastSection(True)
        self.steps.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.steps.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self.steps.setEditTriggers(QTableWidget.NoEditTriggers)
        right.add(self.steps)
        self.result_label = label(
            "任务：未开始\n角度判定：待测量\n对位分析：待 SOMA", wrap=True
        )
        right.add(self.result_label)
        self.stop_button = QPushButton("停止后续步骤", clicked=app.stop)
        right.add(self.stop_button)
        split.addWidget(right)
        split.setSizes([300, 470, 380])
        self.sample = app.sample_edit
        self.sample.textChanged.connect(self.invalidate)
        app.operator_edit.textChanged.connect(self.invalidate)

    def library(self):
        return self.app.pages["recipes"].library

    def on_show(self):
        lib = self.library()
        old = self.recipes.currentData()
        self.recipes.blockSignals(True)
        self.recipes.clear()
        for r in lib.recipes():
            for v in reversed(r["revisions"]):
                if v["status"] == "archived":
                    continue
                self.recipes.addItem(
                    f"{r['name']} · Rev.{v['revision']:03d} · "
                    + (
                        "演示"
                        if v["body"].get("example")
                        else "已放行" if v["status"] == "released" else "草稿"
                    ),
                    (r["recipe_id"], v["revision"]),
                )
        idx = next(
            (i for i in range(self.recipes.count()) if self.recipes.itemData(i) == old),
            -1,
        )
        self.recipes.setCurrentIndex(max(idx, 0))
        self.recipes.blockSignals(False)
        selected = self.recipes.currentData()
        version = lib.get(*selected) if selected else None
        if selected != self._selection or (
            version and version["content_sha256"] != self._display_hash
        ):
            self.selection_changed()

    def select_recipe(self, rid, rev, *, mode="engineering", step=None):
        self.on_show()
        i = next(
            (
                i
                for i in range(self.recipes.count())
                if self.recipes.itemData(i) == (rid, rev)
            ),
            -1,
        )
        self.recipes.setCurrentIndex(i)
        self.mode.setCurrentIndex(self.mode.findData(mode))
        self._scope = step
        self.selection_changed()

    def demo(self):
        page = self.app.pages["recipes"]
        page.seed_demo()
        self.app.connect_demo("sequence")
        self.select_recipe(page.current_id, page.current_rev, mode="simulation")
        self.app.navigate("sequence")

    def load_demo_defaults(self):
        pass

    def persist(self):
        # Output path is a preference, not a process recipe.
        self.app.preferences.update(image_output=self.output.text())

    def rows(self):
        return []

    def invalidate(self, *_):
        self._checked = None
        if hasattr(self, "path_confirm"):
            self.path_confirm.setChecked(False)
            self.load_confirm.setChecked(False)
        if hasattr(self, "status"):
            self.status.setText("配置或任务信息已变化，请重新检查。")
        self.refresh_enabled()

    def selection_changed(self, *_):
        new = self.recipes.currentData()
        if new != self._selection:
            self._scope = None
        self._selection = new
        self.invalidate()
        self._plan = None
        if not self._selection:
            return
        try:
            self._plan = self.build_plan()
            p = self._plan
            self.map.plan = p
            self.map.update()
            identity = p.recipe_identity
            self._display_hash = identity["content_sha256"]
            self.version_label.setText(
                f"{p.name}\nRev.{identity['revision']:03d} · "
                + ("演示配方" if p.simulated_configuration else "现场配方")
            )
            self.summary.setText(
                f"测角点：{len(p.tilt.targets) if p.measure_enabled else 0}\n独立孔位：{sum(s.capture is not None for s in p.stations)}\n转场 Z：{p.safe_z_m*1e6:g} µm\n坐标：设备绝对坐标"
            )
            self.preview(p)
        except (ValueError, TypeError, KeyError) as e:
            self.summary.setText("所选配方尚不满足当前执行模式")
            self.version_label.setText("请检查配方与模式")
            self.map.plan = None
            self.map.update()
            self.steps.setRowCount(0)
            self.status.setText(str(e))

    def clear_scope(self):
        self._scope = None
        self.selection_changed()

    def build_plan(self):
        if not self._selection:
            raise ValueError("请先新建或选择配方")
        w = self.app.workflow
        sim = bool(w and w.simulated)
        p = compile_revision(
            self.library(),
            *self._selection,
            mode=self.mode.currentData(),
            simulated=sim,
        )
        if self._scope:
            if self.mode.currentData() == "production":
                raise ValueError("正式运行须选择完整任务")
            body = self.library().get(*self._selection)["body"]
            nodes = [
                child
                for n in body["step_tree"]
                for child in [n] + n.get("children", [])
            ]
            node = next((n for n in nodes if n["step_id"] == self._scope), None)
            if not node:
                raise ValueError("选中的步骤不存在")
            kind = node["kind"]
            if kind not in ("tilt", "hole", "transit", "safe_transfer", "return_z"):
                raise ValueError("此步骤不能单独执行；判定和归档依赖完整任务")
            stations = (
                tuple(s for s in p.stations if s.name == self._scope)
                if kind in ("hole", "transit")
                else ()
            )
            p = replace(
                p,
                stations=stations,
                measure_enabled=kind == "tilt",
                execution_scope=kind,
                recipe_identity=dict(p.recipe_identity, execution_scope=self._scope),
            )
        return p

    def preview(self, p):
        titles = ["设备与起点检查", "ZEN 实验内容核对"]
        if p.measure_enabled:
            titles += ["共聚焦扫描物料角度 · 多点 AF", "测角结果放行"]
        for s in p.stations:
            titles.append(s.name + " · 安全 Z 转场与 XY 到位")
            if s.capture:
                titles.append(s.name + " · 上下孔采图")
        titles += ["返回安全 Z", "结果归档"]
        self._presented = titles
        self.steps.setRowCount(len(titles))
        for r, t in enumerate(titles):
            self.steps.setItem(r, 0, QTableWidgetItem(t))
            self.steps.setItem(r, 1, QTableWidgetItem("待执行"))

        self.steps.resizeRowsToContents()

    def fingerprint(self):
        return canonical_sha256(
            {
                "plan": self.build_plan().to_dict(),
                "sample": self.sample.text(),
                "operator": self.app.operator_edit.text(),
                "output": self.output.text(),
            }
        )

    def preflight(self):
        try:
            w = self.app.workflow
            if not w or not self.app.device_ready() or w.busy:
                raise ValueError("请先连接设备并解除待恢复状态")
            if not w.xy_enabled:
                raise ValueError("自动流程需要电动 XY；无 XY 请使用单点采图测试")
            if (
                not self.output.text().strip()
                or not Path(self.output.text()).expanduser().is_absolute()
            ):
                raise ValueError("请选择图像输出目录的绝对路径")
            if (
                Path(self.output.text())
                .expanduser()
                .resolve()
                .is_relative_to(self.app.data_root.resolve())
            ):
                raise ValueError("图像输出目录必须位于运行记录目录之外")
            p = self.build_plan()
            p.validate(w.validate)
            validate_capabilities(w.device, p.measure_enabled)
            if self.mode.currentData() == "simulation" and not w.simulated:
                raise ValueError("模拟模式必须连接离线演示设备")
            if self.mode.currentData() != "simulation" and w.simulated:
                raise ValueError("当前是模拟设备，请选择离线模拟模式")
            if (
                not self.sample.text().strip()
                or not self.app.operator_edit.text().strip()
            ):
                raise ValueError("请填写样品编号和操作员")
            token = self.fingerprint()

            def query(d):
                from zen_hardware import Motion
                from math import isfinite

                if d.motion_state() is not Motion.IDLE:
                    raise ValueError("XY 未空闲")
                xy = d.read_xy()
                z = d.read_z()
                optics = d.read_optics()
                if (
                    not all(isfinite(v) for v in (*xy, z))
                    or not p.z_min_m <= z <= p.z_max_m
                ):
                    raise ValueError("位置回读无效或当前 Z 超出任务范围")
                return {
                    "token": token,
                    "device": id(d),
                    "xy": xy,
                    "z_m": z,
                    "optics": optics,
                }

            w.read_only("run_center_preflight", query)
        except (ValueError, RuntimeError, TypeError) as e:
            self.status.setText(str(e))

    def on_rpc_result(self, tag, value):
        if tag != "run_center_preflight":
            return
        if value["token"] == self.fingerprint() and value["device"] == id(
            self.app.workflow.device
        ):
            self._checked = value
            self.status.setText(
                "只读检查通过。启动后核对实验内容；请确认装夹、路线和焦面。"
            )
            self.refresh_enabled()

    def on_rpc_error(self, tag, error):
        if tag == "run_center_preflight":
            self._checked = None
            self.status.setText(str(error))

    def start(self):
        try:
            checked = self._checked
            w = self.app.workflow
            if (
                not checked
                or checked["token"] != self.fingerprint()
                or checked["device"] != id(w.device)
            ):
                raise ValueError("请重新执行检查")
            p = self.build_plan()
            if not self.path_confirm.isChecked() or not self.load_confirm.isChecked():
                raise ValueError("请确认全部路线、装夹和实验")
            self.persist()
            w.start_sequence(
                p,
                self.output.text(),
                confirmed=True,
                experiment_loading_confirmed=True,
                confirmed_start=checked["xy"],
                session={
                    "sample": self.sample.text(),
                    "operator": self.app.operator_edit.text(),
                },
                preflight=checked,
                attestations=[
                    {
                        "key": "route_and_experiment",
                        "confirmed": True,
                        "method": "operator_external_check",
                        "operator": self.app.operator_edit.text(),
                    }
                ],
            )
            self._running_identity = p.recipe_identity
            self._trial_recorded = False
            self._checked = None
            self.preview(p)
            self.status.setText(
                "工程试运行"
                if self.mode.currentData() == "engineering"
                else "任务已启动"
            )
        except (ValueError, RuntimeError, TypeError) as e:
            self.status.setText(str(e))

    def on_workflow_event(self, kind, data, w):
        if kind != "sequence_progress":
            return
        report = data[0]
        self._last_report = report
        states = {
            "running": "执行中",
            "complete": "完成",
            "failed": "失败",
            "stopped": "中止",
        }
        for s in report["steps"]:
            title = s["title"]
            r = (
                self._presented.index(title)
                if title in self._presented
                else self.steps.rowCount()
            )
            if r == self.steps.rowCount():
                self.steps.insertRow(r)
                self.steps.setItem(r, 0, QTableWidgetItem(title))
                self._presented.append(title)
            self.steps.setItem(
                r, 1, QTableWidgetItem(states.get(s["state"], s["state"]))
            )
            self.steps.selectRow(r)
        if report["state"] != "running":
            if report["state"] == "complete" and "结果归档" in self._presented:
                self.steps.setItem(
                    self._presented.index("结果归档"), 1, QTableWidgetItem("完成")
                )
            disposition = report.get("disposition")
            self.result_label.setText(
                "任务："
                + states.get(report["state"], report["state"])
                + "\n角度："
                + (
                    "不合格"
                    if disposition == "MEASUREMENT_REJECTED"
                    else (
                        "通过"
                        if report.get("result") and report["state"] == "complete"
                        else "未判定 / 未启用"
                    )
                )
                + "\n对位分析：待 SOMA\n记录："
                + str(w.sequence_run_path)
            )
            self.status.setText(
                report.get("error", {}).get(
                    "message", "流程完成，已保存本次参数快照和结果。"
                )
            )
            identity = report.get("recipe_identity", {})
            if (
                report["state"] == "complete"
                and not report["simulated"]
                and identity.get("execution_mode") == "engineering"
                and not identity.get("execution_scope")
                and not self._trial_recorded
            ):
                try:
                    self.library().record_trial(
                        identity["recipe_id"],
                        identity["revision"],
                        w.sequence_run_path,
                        by=self.app.operator_edit.text(),
                    )
                    self._trial_recorded = True
                except (ValueError, OSError) as e:
                    self.status.setText("运行已结束，验证记录未关联：" + str(e))

    def on_device_changed(self, w):
        self.selection_changed()

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "图像输出目录")
        if path:
            self.output.setText(path)

    def refresh_enabled(self):
        if not hasattr(self, "start_button"):
            return
        w = self.app.workflow
        busy = bool(w and w.busy)
        ready = bool(w and not busy and self.app.device_ready() and w.xy_enabled)
        self.recipes.setEnabled(not busy)
        self.mode.setEnabled(not busy)
        self.output.setEnabled(not busy)
        self.check_button.setEnabled(ready)
        self.stop_button.setEnabled(busy)
        self.path_confirm.setEnabled(ready)
        self.load_confirm.setEnabled(ready)
        self.start_button.setToolTip("须完成执行前检查与两项现场确认")
        self.start_button.setEnabled(
            ready
            and self._checked is not None
            and self.path_confirm.isChecked()
            and self.load_confirm.isChecked()
        )
