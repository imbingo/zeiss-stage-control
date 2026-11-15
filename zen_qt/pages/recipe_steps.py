"""Engineering step editor built on the version store and dirty-state lifecycle."""

from copy import deepcopy
import json
from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QPushButton,
    QComboBox,
    QCheckBox,
    QWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QInputDialog,
    QMessageBox,
    QLabel,
    QMenu,
)
from .recipes import RecipesPage as VersionPage
from .recipe_tools import RecipeTools
from ..widgets import label, hbox
from zen_steps import (
    CATALOG,
    COMMON_FIELDS,
    make_step,
    migrate_program,
    insert_template,
    numeric,
    flatten,
)
from zen_recipe_store import default_body, DRAFT


class RecipesPage(RecipeTools, VersionPage):
    def _build(self):
        super()._build()
        self.template.hide()
        layout = self.recipe_tree.parentWidget().layout()
        index = layout.indexOf(self.recipe_tree)
        self.library_tabs = QTabWidget()
        self.library_tabs.addTab(self.recipe_tree, "配方列表")
        self.catalog = QTreeWidget()
        self.catalog.setHeaderHidden(True)
        self.catalog.itemDoubleClicked.connect(
            lambda item, *_: (
                self.add_kind(item.data(0, Qt.UserRole))
                if item.data(0, Qt.UserRole)
                else None
            )
        )
        groups = {}
        for key, (group, title, fields) in CATALOG.items():
            if group not in groups:
                parent = QTreeWidgetItem([group])
                self.catalog.addTopLevelItem(parent)
                groups[group] = parent
            item = QTreeWidgetItem([title + (" · 待适配" if key == "set_gain" else "")])
            item.setData(0, Qt.UserRole, key)
            groups[group].addChild(item)
            if key == "set_gain":
                item.setToolTip(
                    0, "允许离线配置；未确认官方接口前禁止执行，不使用相机曝光接口代替"
                )
        self.catalog.expandAll()
        self.library_tabs.addTab(self.catalog, "步骤库")
        layout.insertWidget(index, self.library_tabs, 1)
        button = QPushButton("插入模板…", clicked=self.choose_template)
        mid = self.step_tree.parentWidget().layout()
        mid.insertWidget(1, button)
        for widget in self.step_tree.parentWidget().findChildren(QLabel):
            if "保护步骤不可删除" in widget.text():
                widget.setText(
                    "动作可自由编排；到位校验、图像完整性和异常归档由执行器负责。"
                )
        self.from_here_button = QPushButton("从此处试运行", clicked=self.run_from_here)
        self.test_step_button.parentWidget().layout().insertWidget(
            3, self.from_here_button
        )
        self.add_catalog_button = QPushButton(
            "添加所选步骤",
            clicked=lambda: (
                self.add_kind(self.catalog.currentItem().data(0, Qt.UserRole))
                if self.catalog.currentItem()
                else None
            ),
        )
        layout.insertWidget(index + 1, self.add_catalog_button)
        self._points_target = None
        self.install_recipe_tools()

    def refresh_all(self):
        if not self.current_id:
            self._body = None
            self._selected_step_id = None
            self._loading = True
            self.step_tree.clear()
            self._loading = False
            self.refresh_params()
            self.meta_name.setText("请选择或新建配方")
            self.meta_badge.setText("")
            self.meta_parent.setText("")
            self.step_title.setText("请选择步骤")
            self.conditions.clear()
            self.tests.clear()
            for button in (self.save_button, self.check_button, self.release_button):
                button.setEnabled(True)
            return
        if self.current_id and self._body is None:
            v = self.library.get(self.current_id, self.current_rev)
            self._body = migrate_program(v["body"])
            if "program_steps" not in v["body"] and v["status"] == "draft":
                self._dirty = True
        super().refresh_all()
        if self.current_id:
            v = self.library.get(self.current_id, self.current_rev)
            checks = v.get("checks") or {}
            trial = v.get("validation", {}).get("state")
            self.meta_parent.setText(
                "草稿 → 参数检查 "
                + ("✓" if checks.get("result") == "passed" else "待完成")
                + " → 工程试运行 "
                + ("✓" if trial in ("field_trial_passed", "qualified") else "待完成")
                + " → 人工放行 → 正式版"
            )
            self.release_hint.setText(
                "放行条件：保存当前版本、检查通过、同版本完整现场试运行成功、工程师确认。模拟验证不放行现场配方。"
            )

    def _find_step(self, sid, group_id=None):
        if not self._body:
            return None
        if sid == "__common":
            return {
                "step_id": "__common",
                "kind": "common",
                "label": "公共运动与坐标参数",
            }
        return next(
            (
                n
                for _, n in flatten(self._body.get("program_steps", []))
                if n["step_id"] == sid
            ),
            None,
        )

    def refresh_step_tree(self):
        self._loading = True
        self.step_tree.clear()
        common = QTreeWidgetItem(["公共参数"])
        common.setData(0, Qt.UserRole, ("__common", None))
        common.setFlags(common.flags() & ~Qt.ItemIsDragEnabled & ~Qt.ItemIsDropEnabled)
        self.step_tree.addTopLevelItem(common)

        def add(nodes, parent=None):
            for i, node in enumerate(nodes, 1):
                item = QTreeWidgetItem(
                    [
                        f"{i:02d}  "
                        + node.get(
                            "label",
                            CATALOG.get(node.get("kind"), ("", "未知动作", []))[1],
                        )
                    ]
                )
                item.setData(0, Qt.UserRole, (node["step_id"], None))
                item.setToolTip(0, node["step_id"])
                (
                    parent.addChild(item)
                    if parent
                    else self.step_tree.addTopLevelItem(item)
                )
                if node["step_id"] == self._selected_step_id:
                    self.step_tree.setCurrentItem(item)
                add(node.get("children", []), item)
                item.setExpanded(True)

        add(self._body.get("program_steps", []))
        self._loading = False
        if not self._selected_step_id:
            self.step_tree.setCurrentItem(common)

    def refresh_params(self):
        self._loading = True
        self.param_edits = {}
        while self.form.rowCount():
            self.form.removeRow(0)
        n = self._find_step(self._selected_step_id)
        if not n:
            self._loading = False
            return
        kind = n["kind"]
        self.step_title.setText(n["label"])
        self.form.addRow(label("坐标与 Z：µm · 角度：µrad · 时间：s", role="muted"))
        if kind == "common":
            coord = QComboBox()
            coord.addItems(
                list(
                    dict.fromkeys(
                        [
                            self._body.get("coordinate_system", "未声明"),
                            "设备绝对坐标（装夹基准须人工核对）",
                        ]
                    )
                )
            )
            coord.setEnabled(not self._read_only())
            coord.currentTextChanged.connect(
                lambda v: self._set(self._body, "coordinate_system", v)
            )
            self.form.addRow("坐标声明（不执行转换）", coord)
            for key, title in COMMON_FIELDS:
                self._entry(title, self._body["common_params"], key)
            self.form.addRow(
                label(
                    "每次 XY 动作前确认转场 Z；图像采集步骤本身不移动 Z、不自动 AF。",
                    wrap=True,
                )
            )
        elif kind in CATALOG:
            p = n.setdefault("params", {})
            self.form.addRow("步骤编号", label(n["step_id"], role="muted"))
            self._entry("步骤名称", n, "label")
            for key, title, typ, default in CATALOG[kind][2]:
                if typ == "points":
                    self.points_editor(n, key)
                    continue
                if typ == "bool":
                    c = QCheckBox(title)
                    c.setChecked(bool(p.get(key, default)))
                    c.setEnabled(not self._read_only())
                    c.toggled.connect(lambda v, k=key: self._set(p, k, v))
                    self.form.addRow(c)
                elif typ == "capture":
                    c = QComboBox()
                    c.addItem("共聚焦 · RunExperiment", "confocal")
                    c.addItem("相机 · RunSnap", "camera_snap")
                    c.setCurrentIndex(max(0, c.findData(p.get(key, default))))
                    c.setEnabled(not self._read_only())
                    c.currentIndexChanged.connect(
                        lambda _, k=key, control=c: self._set(
                            p, k, control.currentData()
                        )
                    )
                    self.form.addRow(title, c)
                else:
                    e = self._entry(title, p, key, default)
                    if typ in ("number", "optional"):
                        e.setPlaceholderText("数字，或引用 @步骤编号.z_um")
            if kind == "measure_angle":
                self.form.addRow(
                    label(
                        "高度来源：多点 AF 获取 Z。运行中心显示扫描路径和实测 Z 散点。",
                        wrap=True,
                    )
                )
            if kind in ("capture_upper", "capture_lower"):
                self.form.addRow(
                    label(
                        "仅对当前位置采图；实验必须由前置加载步骤准备。Z 定位和 AF 请显式添加。",
                        wrap=True,
                    )
                )
            if kind == "move_xy":
                self.form.addRow(
                    label(
                        "先确认本步/公共转场 Z，再移动 XY。该保护动作会记录在日志中。",
                        wrap=True,
                    )
                )
            if kind == "zstack":
                self.form.addRow(
                    label(
                        "独立多层采集：保存逐层 TIFF 与 stack.json，不收敛为单层。",
                        wrap=True,
                    )
                )
            if kind == "set_gain":
                self.form.addRow(
                    label(
                        "当前未提供已验证的主增益接口，仅允许配置草稿，禁止执行；不使用相机曝光替代。",
                        wrap=True,
                    )
                )
            if kind in ("move_z", "move_xy", "autofocus"):
                self.form.addRow(QPushButton("读取当前位置示教…", clicked=self.teach))
        from zen_step_help import step_help

        self.conditions.setPlainText(step_help(n))
        v = self.library.get(self.current_id, self.current_rev)
        self.tests.setPlainText(
            json.dumps(
                {"checks": v.get("checks"), "validation": v.get("validation")},
                ensure_ascii=False,
                indent=2,
            )
        )
        self._loading = False

    def points_editor(self, node, key):
        p = node["params"]
        rows = p.get(key, [])
        table = QTableWidget(len(rows), 3)
        table.setHorizontalHeaderLabels(["X · µm", "Y · µm", "预期 Z（可空）· µm"])
        table.setMinimumHeight(180)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(str(value)))
        table.setEnabled(not self._read_only())
        self.point_table = table
        self._points_target = (node["step_id"], key)

        def sync():
            p[key] = [
                [table.item(r, c).text() if table.item(r, c) else "" for c in range(3)]
                for r in range(table.rowCount())
            ]
            self.mark_dirty()

        table.itemChanged.connect(lambda *_: sync())
        self.form.addRow(table)
        buttons = QWidget()
        bl = hbox(buttons, margins=0)

        def add():
            table.insertRow(table.rowCount())
            sync()

        def delete():
            if table.currentRow() >= 0:
                table.removeRow(table.currentRow())
                sync()

        bl.addWidget(QPushButton("＋点位", clicked=add))
        bl.addWidget(QPushButton("删除所选点", clicked=delete))
        bl.addWidget(QPushButton("读取当前 XY/Z", clicked=self.teach_tilt))
        buttons.setEnabled(not self._read_only())
        self.form.addRow(buttons)

    def _collect_body(self):
        return deepcopy(self._body)

    def save_new_version(self):
        if not self.current_id:
            self.validation_hint.setText("尚无配方，请点击“新建配方”后设置步骤。")
            return False
        if self._read_only():
            self.validation_hint.setText(
                "该版本只读。请先点击左侧“复制为新草稿”，再修改和保存。"
            )
            return False
        try:
            b = self._collect_body()
            for _, n in flatten(b["program_steps"]):
                for key, _, typ, default in CATALOG[n["kind"]][2]:
                    value = n["params"].get(key, default)
                    if typ in ("number", "positive", "optional") and value not in (
                        "",
                        None,
                    ):
                        n["params"][key] = numeric(value, key, typ == "positive")
                    if typ == "points":
                        n["params"][key] = [
                            [
                                numeric(v, "点位", allow_ref=False) if v != "" else ""
                                for v in row
                            ]
                            for row in value
                        ]
            for key, _ in COMMON_FIELDS:
                if b["common_params"].get(key) not in ("", None):
                    b["common_params"][key] = numeric(
                        b["common_params"][key], key, allow_ref=False
                    )
            self.library.save_draft(
                self.current_id,
                self.current_rev,
                b,
                by=self._operator(),
                note=self.change_note.text(),
            )
            self._body = b
            self._dirty = False
            self.refresh_all()
            return True
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return False

    def change_template(self, *_):
        pass

    def add_step(self):
        if not self.current_id:
            self.new_recipe()
            if not self.current_id:
                return
        if self._read_only():
            self.validation_hint.setText(
                "正式版只读：先点击“复制为新草稿”，再添加步骤。"
            )
            return
        self.add_menu = QMenu(self)
        groups = {}
        self.add_actions = {}
        self.add_groups = groups
        for kind, (group, title, _) in CATALOG.items():
            if group not in groups:
                groups[group] = self.add_menu.addMenu(group)
            action = groups[group].addAction(
                title + (" · 待适配" if kind == "set_gain" else "")
            )
            action.setData(kind)
            self.add_actions[kind] = action
            action.triggered.connect(lambda checked=False, k=kind: self.add_kind(k))
        self.add_group_actions = [menu.menuAction() for menu in groups.values()]
        self.add_menu.popup(QCursor.pos())

    def add_kind(self, kind):
        if not kind:
            return
        if self._read_only():
            self.validation_hint.setText("正式版只读：先复制为新草稿，再添加步骤。")
            return
        n = make_step(kind)
        selected = self._find_step(self._selected_step_id)
        if selected and selected["kind"] == "point_loop":
            selected.setdefault("children", []).append(n)
        else:
            siblings, index = self._siblings(self._selected_step_id)
            siblings.insert(index + 1 if index >= 0 else len(siblings), n)
        self._selected_step_id = n["step_id"]
        self.mark_dirty()
        self.refresh_step_tree()
        self.refresh_params()

    def _siblings(self, sid):
        def search(nodes):
            for i, n in enumerate(nodes):
                if n["step_id"] == sid:
                    return nodes, i
                result = search(n.get("children", []))
                if result:
                    return result

        return search(self._body["program_steps"]) or (self._body["program_steps"], -1)

    def delete_hole(self):
        if self._read_only():
            return
        nodes, i = self._siblings(self._selected_step_id)
        if i >= 0:
            nodes.pop(i)
            self._selected_step_id = None
            self.mark_dirty()
            self.refresh_step_tree()
            self.refresh_params()

    def move_hole(self, delta):
        if self._read_only():
            return
        nodes, i = self._siblings(self._selected_step_id)
        if i >= 0 and 0 <= i + delta < len(nodes):
            nodes[i], nodes[i + delta] = nodes[i + delta], nodes[i]
            self.mark_dirty()
            self.refresh_step_tree()

    def add_hole(self, kind="hole"):
        self.add_kind("move_xy" if kind == "transit" else "capture_upper")

    def new_recipe(self):
        if not self._resolve_dirty():
            return
        name, ok = QInputDialog.getText(self, "新建配方", "配方名称")
        if ok and name.strip():
            b = default_body(name.strip())
            b.update(program_schema=1, program_steps=[])
            self.current_id, self.current_rev = self.library.create(
                name.strip(), self._operator(), body=b
            )
            self._body = None
            self.refresh()

    def seed_demo(self):
        if not self._resolve_dirty():
            return
        b = default_body("演示 · 可编排测角与 mark", example=True)
        b = migrate_program(b)
        self.current_id, self.current_rev = self.library.create(
            b["product_family"], "演示", body=b
        )
        self._body = None
        self._dirty = False
        self.refresh()

    def teach(self):
        w = self.app.workflow
        n = self._find_step(self._selected_step_id)
        if (
            not w
            or w.busy
            or self._read_only()
            or n["kind"] not in ("move_xy", "move_z", "autofocus")
        ):
            return
        token = (
            self.current_id,
            self.current_rev,
            n["step_id"],
            json.dumps(n["params"], sort_keys=True),
        )
        w.read_only(
            "program_teach",
            lambda d: {
                "target": token,
                "xy": d.read_xy() if w.xy_enabled else None,
                "z": d.read_z(),
            },
        )

    def teach_tilt(self):
        if self._read_only() or not self.app.workflow or self.app.workflow.busy:
            return
        row = self.point_table.currentRow()
        if row < 0:
            return
        sid, key = self._points_target
        points = self._find_step(sid)["params"][key]
        token = (self.current_id, self.current_rev, sid, key, row, json.dumps(points))
        if not self.app.workflow.xy_enabled:
            self.validation_hint.setText("当前无 XY，无法示教测角 XY 点")
            return
        self.app.workflow.read_only(
            "program_point_teach",
            lambda d: {"target": token, "xy": d.read_xy(), "z": d.read_z()},
        )

    def on_rpc_result(self, tag, value):
        if tag == "program_teach":
            rid, rev, sid, previous = value["target"]
            n = self._find_step(sid)
            if (
                (rid, rev) != (self.current_id, self.current_rev)
                or self._read_only()
                or not n
                or json.dumps(n["params"], sort_keys=True) != previous
            ):
                return
            if n["kind"] == "move_xy":
                if value["xy"] is None:
                    return
                n["params"].update(x_um=value["xy"][0] * 1e6, y_um=value["xy"][1] * 1e6)
            else:
                n["params"]["z_um" if n["kind"] == "move_z" else "center_um"] = (
                    value["z"] * 1e6
                )
            self.mark_dirty()
            self.refresh_params()
            return
        if tag == "program_point_teach":
            rid, rev, sid, key, row, previous = value["target"]
            n = self._find_step(sid)
            if (
                (rid, rev) != (self.current_id, self.current_rev)
                or self._read_only()
                or not n
                or json.dumps(n["params"][key]) != previous
            ):
                return
            n["params"][key][row] = [
                value["xy"][0] * 1e6,
                value["xy"][1] * 1e6,
                value["z"] * 1e6,
            ]
            self.mark_dirty()
            self.refresh_params()
            return
        super().on_rpc_result(tag, value)

    def run_from_here(self):
        if self.app.action_busy():
            self.validation_hint.setText("设备任务正在执行，请等待结束或停止后测试。")
            return
        if self._dirty and not self.save_new_version():
            return
        if not self.current_id or self._selected_step_id in (None, "__common"):
            self.validation_hint.setText("请先选择具体执行步骤，公共参数不是执行起点。")
            return
        center = self.app.pages["sequence"]
        center.select_recipe(
            self.current_id,
            self.current_rev,
            mode=(
                "simulation"
                if self.app.workflow and self.app.workflow.simulated
                else "engineering"
            ),
            step=self._selected_step_id,
            from_here=True,
        )
        self.app.navigate("sequence")

    def update_enabled(self, v):
        super().update_enabled(v)
        self.check_button.setEnabled(True)
        self.save_button.setEnabled(True)
        self.release_button.setEnabled(True)
        self.release_button.setToolTip(
            "点击查看尚缺的放行条件；满足后才会进入人工放行。"
        )

    def mark_dirty(self, *args):
        super().mark_dirty(*args)
        if hasattr(self, "release_button"):
            self.release_button.setEnabled(True)

    def check_recipe(self):
        if not self.current_id:
            self.validation_hint.setText("请先新建或选择配方，再检查参数。")
            return
        if self._read_only():
            self.validation_hint.setText(
                "该版本只读，检查记录在“验证与测试记录”中；要修改请复制草稿。"
            )
            self.tabs.setCurrentIndex(2)
            return
        super().check_recipe()

    def release_recipe(self):
        if not self.current_id:
            self.validation_hint.setText("请先选择需要放行的草稿。")
            return
        v = self.library.get(self.current_id, self.current_rev)
        if v["status"] != "draft":
            self.validation_hint.setText(
                "当前版本已放行或归档，为只读版本，无需再次放行；要变更请复制为新草稿。"
            )
            self.tabs.setCurrentIndex(2)
            return
        missing = []
        if self._dirty:
            missing.append("保存当前草稿")
        if v["body"].get("example"):
            missing.append("演示配方不能放行现场；请新建现场配方并设置真实参数")
        if (v.get("checks") or {}).get("result") != "passed":
            missing.append("通过“检查配方”")
        if v.get("validation", {}).get("state") != "field_trial_passed":
            missing.append("完成同版本、完整流程的“现场工程试运行”")
        if not self._operator():
            missing.append("填写工程师 / 操作员")
        if missing:
            self.validation_hint.setText("放行尚缺条件：\n• " + "\n• ".join(missing))
            self.tabs.setCurrentIndex(2)
            return
        super().release_recipe()

    def _run(self, mode, step=None):
        if not self.current_id:
            self.validation_hint.setText("请先新建或选择配方。")
            return
        if self.app.action_busy():
            self.validation_hint.setText("设备任务正在执行；请先等待结束或停止。")
            return
        super()._run(mode, step)

    def simulate(self):
        from ..action_feedback import defer_read

        token = lambda: (self.current_id, self.current_rev, self._selected_step_id)
        if defer_read(
            self.app, self, self.simulate, token, self.validation_hint.setText
        ):
            return
        if not self.current_id:
            self.validation_hint.setText("请先新建或选择配方，再模拟当前流程。")
            return
        super().simulate()

    def field_trial(self):
        self._run("engineering")

    def test_step(self):
        if self._selected_step_id in (None, "__common"):
            self.validation_hint.setText(
                "请选择中间执行流程中的具体动作，公共参数本身不执行。"
            )
            return
        self._run(
            (
                "simulation"
                if self.app.workflow and self.app.workflow.simulated
                else "engineering"
            ),
            self._selected_step_id,
        )

    def refresh_enabled(self):
        for name in (
            "simulate_button",
            "trial_button",
            "test_step_button",
            "from_here_button",
            "release_button",
            "check_button",
            "save_button",
        ):
            if hasattr(self, name):
                getattr(self, name).setEnabled(True)
