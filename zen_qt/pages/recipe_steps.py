"""Engineering step editor built on the version store and dirty-state lifecycle."""

from copy import deepcopy
import json
from PySide6.QtCore import Qt, QTimer
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
    QFrame,
    QApplication,
    QDialog,
    QFileDialog,
    QGridLayout,
    QLineEdit,
)
from .recipes import RecipesPage as VersionPage
from .recipe_tools import RecipeTools
from ..widgets import label, hbox
from .. import theme
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
        # Group the action bar by consequence: checks and simulation never
        # touch hardware; the amber group drives the connected device.
        bar = self.test_step_button.parentWidget().layout()
        for button in (
            self.check_button,
            self.test_step_button,
            self.simulate_button,
            self.trial_button,
            self.release_button,
        ):
            bar.removeWidget(button)

        def rule():
            line = QFrame()
            line.setFrameShape(QFrame.VLine)
            line.setStyleSheet(f"color:{theme.LINE};")
            return line

        bar.addWidget(label("不动设备", role="muted"))
        self.check_button.setToolTip("只检查参数、步骤依赖与运动范围，不连接也不驱动设备。")
        self.simulate_button.setToolTip("切到离线演示并在模拟设备上运行，不驱动现场设备。")
        bar.addWidget(self.check_button)
        bar.addWidget(self.simulate_button)
        bar.addWidget(rule())
        self.device_group_label = label("会驱动已连接设备", role="muted")
        self.device_group_label.setStyleSheet(f"color:{theme.WARN};font-weight:600;")
        bar.addWidget(self.device_group_label)
        for button, tip in (
            (self.test_step_button, "只执行选中的一个动作。"),
            (self.from_here_button, "从选中动作执行到流程结束。"),
            (self.trial_button, "在现场设备上按工程试运行执行完整流程。"),
        ):
            button.setObjectName("moving")
            button.setToolTip(
                tip + "连接现场设备时会下发运动 / 采集；启动前仍需在运行中心核对并确认。"
            )
            bar.addWidget(button)
        bar.addWidget(rule())
        bar.addWidget(self.release_button)
        self._deleted = None
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
            if getattr(self, "_deleted", None) and self._deleted[0] != (
                self.current_id,
                self.current_rev,
            ):
                self._forget_deleted()
            self.refresh_progress()
            self.release_hint.setText(
                "放行条件：保存当前版本、检查通过、同版本完整工程试运行成功、工程师确认。模拟验证不放行现场配方。"
            )

    def body_changed(self):
        """True when the edited steps differ from the saved revision (not just the note)."""
        from zen_records import canonical_sha256

        v = self.library.get(self.current_id, self.current_rev) if self.current_id else None
        if not v or not self._dirty or self._body is None:
            return False
        try:
            return canonical_sha256(self._body) != v["content_sha256"]
        except (TypeError, ValueError):
            return True

    def refresh_progress(self):
        v = self.library.get(self.current_id, self.current_rev)
        if v["status"] != DRAFT:
            self.meta_parent.setText("已放行 / 归档版本 · 只读")
            self.meta_parent.setStyleSheet("")
            return
        checks = v.get("checks") or {}
        trial = v.get("validation", {}).get("state")
        stale = self.body_changed()
        if stale:
            # The saved verdicts describe the previous content; saving
            # discards them, so do not keep showing a tick meanwhile.
            check_text = trial_text = "需重做"
        else:
            check_text = (
                "✓"
                if checks.get("result") == "passed"
                else "未通过" if checks.get("result") == "failed" else "待完成"
            )
            trial_text = (
                "✓" if trial in ("field_trial_passed", "qualified") else "待完成"
            )
        self.meta_parent.setText(
            ("草稿（已修改，未保存）" if stale else "草稿")
            + f" → 参数检查 {check_text} → 工程试运行 {trial_text} → 人工放行 → 正式版"
        )
        self.meta_parent.setStyleSheet(
            f"color:{theme.WARN};font-weight:600;" if stale else ""
        )
        if stale and (checks or trial not in (None, "unvalidated")):
            self.validation_hint.setText(
                "配方内容已修改：之前的参数检查和试运行结论不再适用，保存后需重新检查。"
            )

    @staticmethod
    def describe_validation(v):
        """Operator-readable check / trial record instead of the stored JSON."""

        def when(text):
            return str(text or "").replace("T", " ")[:19] or "时间未记录"

        results = {"passed": "通过", "failed": "未通过"}
        lines = []
        checks = v.get("checks")
        if not checks:
            lines.append("参数检查：尚未检查。点击下方“检查配方”。")
        else:
            lines.append(
                f"参数检查：{results.get(checks.get('result'), checks.get('result'))}"
                f" · {when(checks.get('checked_at'))}"
            )
            for item in checks.get("items", []):
                ok = item.get("result") == "passed"
                lines.append(
                    f"  {'✓' if ok else '✗'} {item.get('label', item.get('key', ''))}"
                    + (f"：{item['detail']}" if item.get("detail") else "")
                )
            for warning in checks.get("warnings", []):
                lines.append(f"  ! {warning}")
        lines.append("")
        validation = v.get("validation") or {}
        state = validation.get("state")
        if state in ("field_trial_passed", "qualified"):
            lines.append(
                f"现场工程试运行：通过 · {when(validation.get('at'))}"
                f" · 操作员 {validation.get('by') or '未记录'}"
            )
            count = len(validation.get("experiment_fingerprints") or {})
            lines.append(f"  已记录 {count} 个 ZEN 实验指纹")
            if validation.get("run_path"):
                lines.append(f"  运行记录：{validation['run_path']}")
        else:
            lines.append(
                "现场工程试运行：尚未完成。需在现场设备上用本版本完整运行一次；模拟运行不计入。"
            )
        lines.append("")
        status = v.get("status")
        if status == DRAFT:
            lines.append("放行：未放行（草稿）")
        else:
            lines.append(
                "放行：" + {"released": "已放行，正式版只读"}.get(status, "已归档，只读")
            )
        return "\n".join(lines)

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

    def queue_params_refresh(self):
        # Never delete a sender while Qt is still emitting its value-change signal.
        stamp=(id(self._body),self.current_id,self.current_rev,self._selected_step_id)
        token=object();self._params_refresh_token=token
        def refresh():
            if (getattr(self,"_params_refresh_token",None) is token
                and stamp==(id(self._body),self.current_id,self.current_rev,self._selected_step_id)):
                self._params_refresh_token=None
                self.refresh_params()
        QTimer.singleShot(0,self,refresh)

    def refresh_params(self):
        self._loading = True
        self.param_edits = {}
        while self.form.rowCount():
            self.form.removeRow(0)
        n = self._find_step(self._selected_step_id)
        if not n:
            # Nothing selected: do not leave the previous step's title and help on screen.
            self.step_title.setText("请选择步骤")
            self.conditions.clear()
            self.tests.clear()
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
                if kind == "measure_heights" and p.get("xy_mode", "motorized") == "current" and key in ("prescan_half_um", "prescan_timeout_s", "max_prediction_residual_um"):
                    continue
                if kind == "measure_heights" and key in ("prescan_timeout_s", "timeout_s", "max_prediction_residual_um") and not getattr(self,"_height_advanced",False):
                    continue
                if kind == "measure_heights" and key == "points_um":
                    advanced=QCheckBox("更多 AF 设置（超时、拟合残差）")
                    advanced.setChecked(getattr(self,"_height_advanced",False))
                    advanced.toggled.connect(lambda checked:(setattr(self,"_height_advanced",checked),self.queue_params_refresh()))
                    self.form.addRow(advanced)
                    self.form.addRow(label(f"共用超时：预扫描 {p.get('prescan_timeout_s',60)} s / 精扫描 {p.get('timeout_s',30)} s；残差上限 {p.get('max_prediction_residual_um',2)} µm",wrap=True,role="muted"))
                if typ in ("surface_count", "height_mode"):
                    control = QComboBox()
                    choices = [("两面",2),("三面",3)] if typ == "surface_count" else [("XY 路径 · 四点预测","motorized"),("固定当前位置 · 单点（不预测）","current")]
                    for text,value in choices:control.addItem(text,value)
                    control.setCurrentIndex(max(0,control.findData(p.get(key,default))))
                    control.setEnabled(not self._read_only())
                    control.currentIndexChanged.connect(lambda _,c=control,k=key: (self._set(p,k,c.currentData()),self.queue_params_refresh()))
                    self.form.addRow(title,control)
                    continue
                if typ == "height_faces":
                    self.height_faces_editor(n)
                    continue
                if typ == "points":
                    if kind == "measure_heights" and p.get("xy_mode")=="current":continue
                    self.points_editor(n, key)
                    continue
                if typ == "bool":
                    c = QCheckBox(title)
                    c.setChecked(bool(p.get(key, default)))
                    c.setEnabled(not self._read_only())
                    c.toggled.connect(lambda v, k=key: self._set(p, k, v))
                    if key == "predict_plane":
                        c.toggled.connect(lambda _: self.queue_params_refresh())
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
            if kind == "measure_heights":
                self.form.addRow(label("逐点 Z1→Z2→Z3，各面独立四点预测；只输出实测高度与差值，不扣物料厚度。固定点不调用 XY 运动，无 XY 时不读取 XY。正常完成返回公共安全 Z，异常或停止不自动返回。",wrap=True))
            if kind == "measure_angle":
                self.form.addRow(QPushButton("读取当前 Z 为初始搜索中心（只读）", clicked=self.teach_initial_z))
                self.form.addRow(
                    label(
                        "填写 ZEN 中已保存的实验名称，不引用独立测角测试 Recipe。预测模式：先测勾选的四点，再按固定预测平面测剩余点；四点使用初始 Z，精扫描忽略表中预期 Z。预测值不参与最终测角，未标记点使用精扫描范围。四点应分布在区域四周；超时不自动重试。",
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
        self.tests.setPlainText(self.describe_validation(v))
        self._loading = False

    def height_faces_editor(self, node):
        from zen_heights import default_surfaces, AF_DEFAULTS
        p=node["params"];faces=p.setdefault("surfaces",default_surfaces())
        self.height_z_edits={}
        table=QTableWidget(p.get("surface_count",3),5)
        table.setHorizontalHeaderLabels(["面","ZEN 实验名称","初始 Z · µm","示教","独立 AF"])
        table.verticalHeader().setVisible(False)
        table.setMinimumHeight(180 if p.get("surface_count",3)==3 else 140)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        header=table.horizontalHeader();header.setSectionResizeMode(1,QHeaderView.Stretch)
        for col,width in ((0,40),(2,100),(3,65),(4,70)):
            header.setSectionResizeMode(col,QHeaderView.Fixed);table.setColumnWidth(col,width)
        for index,face in enumerate(faces[:p.get("surface_count",3)]):
            table.setRowHeight(index,44)
            table.setItem(index,0,QTableWidgetItem(f"Z{index+1}"))
            for col,key in ((1,"experiment"),(2,"z_um")):
                edit=QLineEdit(str(face.get(key,"")));edit.setEnabled(not self._read_only())
                edit.setPlaceholderText("ZEN 名称" if col==1 else "只读示教 / 输入")
                edit.textChanged.connect(lambda value,f=face,k=key:self._set(f,k,value))
                table.setCellWidget(index,col,edit)
                if key=="z_um":self.height_z_edits[index]=edit
            button=QPushButton("读取",clicked=lambda checked=False,i=index:self.teach_height_z(i))
            button.setToolTip("只读取当前 Z，不运动、不读 XY")
            button.setEnabled(not self._read_only());table.setCellWidget(index,3,button)
            override=QCheckBox("独立");override.setObjectName(f"height_override_{index}")
            override.setToolTip("展开本面的独立 AF 参数；未勾选时使用共用参数")
            override.setChecked(bool(face.get("override")));override.setEnabled(not self._read_only())
            override.toggled.connect(lambda value,f=face:self.set_height_override(p,f,value))
            table.setCellWidget(index,4,override)
        self.form.addRow(table)
        for index,face in enumerate(faces[:p.get("surface_count",3)]):
            if face.get("override"):
                self.form.addRow(label(f"Z{index+1} 独立 AF 参数",role="h3"))
                for key,default in AF_DEFAULTS.items():
                    if p.get("xy_mode")=="current" and key.startswith("prescan"):continue
                    titles={"prescan_half_um":"预扫描 AF 半范围 · µm","prescan_timeout_s":"预扫描超时 · s","half_um":"精扫描 / 单点 AF 半范围 · µm","timeout_s":"精扫描 / 单点超时 · s"}
                    self._entry(titles[key],face,key,p.get(key,default))

    def set_height_override(self,params,face,enabled):
        from zen_heights import AF_DEFAULTS
        if self._read_only():return
        if enabled:
            for key,default in AF_DEFAULTS.items():face.setdefault(key,params.get(key,default))
        self._set(face,"override",enabled);self.queue_params_refresh()

    def teach_height_z(self, index):
        node=self._find_step(self._selected_step_id);w=self.app.workflow
        if not node or node["kind"]!="measure_heights" or self._read_only():return
        if not w or w.busy:
            self.validation_hint.setText("请先连接设备并等待读取/任务结束，再点击示教；未发送运动。")
            return
        stamp=(self.current_id,self.current_rev,node["step_id"],index,json.dumps(node["params"],sort_keys=True))
        generation=getattr(w,"action_generation",0)
        w.read_only("heights_teach",lambda d:dict(target=stamp,device=id(d),generation=generation,z=d.read_z()))

    def points_editor(self, node, key):
        p = node["params"]
        rows = p.get(key, [])
        angle = node["kind"] in ("measure_angle", "measure_heights")
        table = QTableWidget(len(rows), 4 if angle else 3)
        table.setHorizontalHeaderLabels(["X · µm", "Y · µm", "固定模式预期 Z（可空）· µm"] + (["预扫描点"] if angle else []))
        if node["kind"] == "measure_heights":table.setColumnHidden(2,True)
        table.setMinimumHeight(180)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                item = QTableWidgetItem(str(value))
                if c == 2 and angle and p.get("predict_plane"):
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setToolTip("预测模式不使用此值；预扫描使用初始 Z，精扫描由四点平面计算")
                table.setItem(r, c, item)
        if angle:
            for r in range(len(rows)):
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                check.setCheckState(Qt.Checked if r in p.get("prescan_indices", []) else Qt.Unchecked)
                table.setItem(r, 3, check)
        table.setEnabled(not self._read_only())
        self.point_table = table
        self._points_target = (node["step_id"], key)

        def sync():
            p[key] = [
                [table.item(r, c).text() if table.item(r, c) else "" for c in range(3)]
                for r in range(table.rowCount())
            ]
            if angle:
                p["prescan_indices"] = [r for r in range(table.rowCount())
                    if table.item(r,3) and table.item(r,3).checkState() == Qt.Checked]
            self.mark_dirty()

        table.itemChanged.connect(lambda *_: sync())
        self.form.addRow(table)
        buttons = QWidget()
        bl = hbox(buttons, margins=0)

        def add():
            table.insertRow(table.rowCount())
            if angle:
                item = QTableWidgetItem()
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                table.setItem(table.rowCount()-1, 3, item)
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
        tools = QWidget(); layout = QGridLayout(tools); layout.setContentsMargins(0,0,0,0)
        layout.addWidget(QPushButton("批量生成路径…", clicked=lambda: self.generate_points(node,key)),0,0)
        layout.addWidget(QPushButton("从 Excel 粘贴…", clicked=lambda: self.paste_points(node,key)),0,1)
        csv_button = QPushButton("CSV…")
        csv_menu = QMenu(csv_button)
        csv_menu.addAction("导入 CSV（µm）…", lambda: self.import_points(node,key))
        csv_menu.addAction("导出 CSV（µm）…", lambda: self.export_points(node,key))
        csv_button.setMenu(csv_menu); layout.addWidget(csv_button,1,0)
        if angle:
            layout.addWidget(QPushButton("选择四角预扫描点", clicked=lambda: self.select_corners(node,key)),1,1)
        tools.setEnabled(not self._read_only()); self.form.addRow(tools)

    def replace_points(self, node, key, points, append=False):
        if self._read_only() or self._find_step(node["step_id"]) is not node:
            raise ValueError("配方已变化或当前版本只读，未应用路径")
        from zen_scan_path import parse_points
        combined = (node["params"].get(key, []) if append else []) + points
        # Validate combined rows, including duplicates at append boundaries.
        checked = parse_points("\n".join("\t".join(str(v) for v in row) for row in combined))
        node["params"][key] = checked
        node["params"]["prescan_indices"] = []
        self.mark_dirty(); self.refresh_params()
        self.validation_hint.setText("路径已应用到草稿，未移动设备；请重新选择预扫描点、保存并检查配方。")

    def generate_points(self, node, key):
        if self._read_only(): return
        from .path_tools import PathDialog
        self.path_dialog = PathDialog(self)
        if self.path_dialog.exec() == QDialog.Accepted:
            try:
                self.replace_points(node,key,self.path_dialog.points,self.path_dialog.apply_mode.currentIndex()==1)
            except ValueError as e:
                self.validation_hint.setText(str(e))

    def import_points(self, node, key):
        if self._read_only(): return
        file, _ = QFileDialog.getOpenFileName(self,"导入设备绝对坐标 CSV · µm","","CSV (*.csv)")
        if file:
            try:
                from pathlib import Path
                self.paste_points(node,key,Path(file).read_text(encoding="utf-8-sig"))
            except (OSError,UnicodeError) as e: self.validation_hint.setText("导入失败："+str(e))

    def export_points(self, node, key):
        from zen_scan_path import parse_points
        try:
            points = parse_points("\n".join("\t".join(str(v) for v in row) for row in node["params"].get(key,[])))
            file, _ = QFileDialog.getSaveFileName(self,"导出设备绝对坐标 CSV · µm","scan_path_um.csv","CSV (*.csv)")
            if file:
                from pathlib import Path
                Path(file).write_text("X_um,Y_um,Z_um\n"+"\n".join(",".join(str(v) for v in row) for row in points),encoding="utf-8-sig")
        except (ValueError,OSError) as e: self.validation_hint.setText("导出失败："+str(e))

    def paste_points(self, node, key, text=None):
        if self._read_only(): return
        from zen_scan_path import parse_points
        text, ok = QInputDialog.getMultiLineText(self,"粘贴点位 · µm","粘贴 Excel 两列 X/Y 或三列 X/Y/Z（无表头）。应用后替换当前路径，清除预扫描选择。",QApplication.clipboard().text() if text is None else text)
        if ok:
            try: self.replace_points(node,key,parse_points(text))
            except ValueError as e: self.validation_hint.setText("粘贴未应用："+str(e))

    def select_corners(self, node, key):
        if self._read_only(): return
        try:
            rows = node["params"][key]
            xs,ys=zip(*[(float(r[0]),float(r[1])) for r in rows])
            targets=[(x,y) for y in (min(ys),max(ys)) for x in (min(xs),max(xs))]
            indices=[next(i for i,r in enumerate(rows) if (float(r[0]),float(r[1]))==target) for target in targets]
            from zen_scan_path import validate_prescan
            validate_prescan(rows,indices)
            node["params"]["prescan_indices"] = sorted(indices)
            self.mark_dirty(); self.refresh_params()
        except (ValueError,StopIteration):
            self.validation_hint.setText("路径没有四个矩形角点，请手动勾选四个分布合适的点。")

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
        # One level: twelve actions under section headings, no submenus.
        self.add_menu = QMenu(self)
        self.add_actions = {}
        seen = set()
        for kind, (group, title, _) in CATALOG.items():
            if group not in seen:
                seen.add(group)
                self.add_menu.addSection(group)
            action = self.add_menu.addAction(
                title + (" · 待适配" if kind == "set_gain" else "")
            )
            action.setData(kind)
            self.add_actions[kind] = action
            action.triggered.connect(lambda checked=False, k=kind: self.add_kind(k))
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
        if i < 0:
            self.validation_hint.setText("请先在执行流程中选中要删除的动作；公共参数不能删除。")
            return
        node = nodes[i]
        inner = sum(1 for _ in flatten(node.get("children", [])))
        if inner and (
            QMessageBox.question(
                self,
                "删除步骤",
                f"“{node['label']}”包含 {inner} 个子步骤，将一并删除。\n删除后可用“撤销删除”恢复一次。",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            != QMessageBox.Yes
        ):
            return
        parent = next(
            (
                n["step_id"]
                for _, n in flatten(self._body["program_steps"])
                if n.get("children") is nodes
            ),
            None,
        )
        nodes.pop(i)
        self._deleted = ((self.current_id, self.current_rev), parent, i, node)
        self.undo_button.setEnabled(True)
        self.undo_button.setToolTip(f"恢复刚删除的“{node['label']}”")
        self.validation_hint.setText(
            f"已删除“{node['label']}”"
            + (f"及 {inner} 个子步骤" if inner else "")
            + "。点“撤销删除”可恢复。"
        )
        self._selected_step_id = None
        self.mark_dirty()
        self.refresh_step_tree()
        self.refresh_params()

    def _forget_deleted(self):
        self._deleted = None
        self.undo_button.setEnabled(False)
        self.undo_button.setToolTip("")

    def undo_delete(self):
        if not self._deleted or self._read_only():
            return
        _, parent, index, node = self._deleted
        holder = self._find_step(parent) if parent else None
        nodes = (
            holder.setdefault("children", [])
            if holder
            else self._body["program_steps"]
        )
        nodes.insert(min(index, len(nodes)), node)
        self._forget_deleted()
        self._selected_step_id = node["step_id"]
        self.validation_hint.setText(f"已恢复“{node['label']}”。")
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

    def teach_initial_z(self):
        w = self.app.workflow
        node = self._find_step(self._selected_step_id)
        if self._read_only() or not node or node["kind"] != "measure_angle": return
        if not w or w.busy:
            self.validation_hint.setText("请连接设备并等待读取/任务结束后重新点击；未下发运动。")
            return
        token = (self.current_id,self.current_rev,node["step_id"],json.dumps(node["params"],sort_keys=True))
        generation = getattr(w,"action_generation",0)
        w.read_only("prediction_initial_z",lambda d:dict(target=token,device=id(d),generation=generation,z=d.read_z()))

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

    def on_rpc_error(self, tag, error):
        if tag == "path_endpoint":
            dialog = getattr(self, "path_dialog", None)
            if dialog and dialog.isVisible(): dialog.notice.setText("只读示教失败："+str(error))
        elif tag in ("prediction_initial_z", "heights_teach"):
            self.validation_hint.setText("初始 Z 读取失败："+str(error))
        else:
            super().on_rpc_error(tag, error)

    def on_rpc_result(self, tag, value):
        if tag == "heights_teach":
            rid,rev,sid,index,params=value["target"];node=self._find_step(sid);w=self.app.workflow
            if (not w or id(w.device)!=value["device"] or getattr(w,"action_generation",0)!=value["generation"]
                or (rid,rev)!=(self.current_id,self.current_rev) or not node or self._read_only()
                or json.dumps(node["params"],sort_keys=True)!=params):return
            try:
                node["params"]["surfaces"][index]["z_um"]=numeric(value["z"]*1e6,"初始 Z",allow_ref=False)
                self.mark_dirty();self.refresh_params()
            except ValueError as error:self.validation_hint.setText(str(error))
            return
        if tag == "prediction_initial_z":
            rid,rev,sid,params = value["target"]
            w = self.app.workflow; node = self._find_step(sid)
            if (not w or id(w.device) != value["device"] or getattr(w,"action_generation",0) != value["generation"]
                or (rid,rev) != (self.current_id,self.current_rev) or self._read_only() or not node
                or json.dumps(node["params"],sort_keys=True) != params): return
            try:
                node["params"]["z_um"] = numeric(value["z"]*1e6,"初始 Z",allow_ref=False)
                self.mark_dirty(); self.refresh_params()
            except ValueError as e: self.validation_hint.setText(str(e))
            return
        if tag == "path_endpoint":
            dialog = getattr(self, "path_dialog", None)
            if dialog: dialog.on_result(value)
            return
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
        if not self._loading and self.current_id and hasattr(self, "_deleted"):
            self.refresh_progress()

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
