"""Three-pane, model-backed recipe editor. Every edit belongs to a saved revision."""

from copy import deepcopy
import json
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QLineEdit,
    QPushButton,
    QComboBox,
    QTabWidget,
    QFormLayout,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QMessageBox,
    QInputDialog,
    QSplitter,
    QScrollArea,
    QCheckBox,
    QFileDialog,
    QDialog,
    QDialogButtonBox,
)
from zen_recipe_store import (
    RecipeLibrary,
    default_body,
    DRAFT,
    RELEASED,
    ARCHIVED,
    STATUS_NAMES,
)
from ..widgets import Card, hbox, vbox, label
from .device import _page

COMMON = {
    "safe_z_um": "安全转场 Z · µm",
    "z_min_um": "任务 Z 下限 · µm",
    "z_max_um": "任务 Z 上限 · µm",
    "xy_tolerance_um": "XY 到位容差 · µm",
    "z_tolerance_um": "Z 到位容差 · µm",
    "settle_s": "稳定等待 · s",
    "capture_timeout_s": "采图超时 · s",
    "upper_experiment": "默认上孔实验",
    "lower_experiment": "默认下孔实验",
}
HOLE = {
    "x_um": "X · µm",
    "y_um": "Y · µm",
    "upper_z_um": "上孔 Z · µm",
    "lower_z_um": "下孔 Z · µm",
    "upper_experiment": "上孔实验（留空继承）",
    "lower_experiment": "下孔实验（留空继承）",
    "upper_half_um": "上孔 AF 半范围 · µm",
    "lower_half_um": "下孔 AF 半范围 · µm",
}


class RecipesPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.library = RecipeLibrary(app.data_root / "recipes.json")
        self.current_id = self.current_rev = None
        self._body = None
        self._dirty = False
        self._loading = False
        self._selected_step_id = None
        self.param_edits = {}
        self.experiment_names = []
        # Previous preview seeds had been released without field validation: quarantine honestly.
        migrated = False
        for r in self.library.recipes():
            for v in r["revisions"]:
                if v.get("created_by") == "示例数据" and not v["body"].get("example"):
                    v["body"]["example"] = True
                    v["status"] = DRAFT
                    from zen_records import canonical_sha256

                    v["content_sha256"] = canonical_sha256(v["body"])
                    v["checks"] = None
                    v["validation"] = {"state": "unvalidated"}
                    migrated = True
        if migrated:
            self.library.save()
        self._build()
        self.refresh()
        if self.library.error:
            QMessageBox.warning(self, "配方库读取失败", self.library.error)

    def _build(self):
        lay, head = _page(self, "配方管理", "选择配方 → 编排流程 → 设置步骤参数")
        for text, fn in [
            ("新建配方", self.new_recipe),
            ("导入旧配置", self.import_legacy),
            ("载入演示配方", self.seed_demo),
        ]:
            head.addWidget(QPushButton(text, clicked=fn))
        meta = QWidget()
        ml = hbox(meta, margins=0)
        self.meta_name = label("请选择或新建配方")
        self.meta_badge = label("")
        self.meta_parent = label("", role="muted")
        for c in [self.meta_name, self.meta_badge, self.meta_parent]:
            ml.addWidget(c)
        ml.addStretch()
        self.compare_button = QPushButton("版本比较", clicked=self.compare_versions)
        self.save_button = QPushButton("保存草稿", clicked=self.save_new_version)
        self.save_button.setObjectName("primary")
        ml.addWidget(self.compare_button)
        ml.addWidget(self.save_button)
        lay.addWidget(meta)
        split = QSplitter()
        lay.addWidget(split, 1)
        left = Card("配方与版本")
        left.setMinimumWidth(210)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索产品 / 配方")
        self.search.textChanged.connect(self.refresh_tree)
        left.add(self.search)
        self.recipe_tree = QTreeWidget()
        self.recipe_tree.setHeaderHidden(True)
        self.recipe_tree.itemSelectionChanged.connect(self.on_version_selected)
        left.add(self.recipe_tree)
        left.add(QPushButton("复制为新草稿", clicked=self.copy_draft))
        self.info_status = label("")
        self.info_author = label("", role="muted")
        left.add(self.info_status)
        left.add(self.info_author)
        self.change_note = QLineEdit()
        self.change_note.setPlaceholderText("变更说明：改了什么、为什么")
        self.change_note.textEdited.connect(self.mark_dirty)
        left.add(self.change_note)
        left.add(label("已放行版本只读；修改时生成新草稿。", wrap=True, role="muted"))
        split.addWidget(left)
        mid = Card("执行流程")
        mid.setMinimumWidth(260)
        self.template = QComboBox()
        self.template.addItems(["测角 → 判定 → 上下孔采图", "仅上下孔采图"])
        self.template.currentTextChanged.connect(self.change_template)
        mid.add(self.template)
        row = QWidget()
        rl = hbox(row, margins=0)
        for text, fn in [
            ("＋步骤", self.add_step),
            ("↑", lambda: self.move_hole(-1)),
            ("↓", lambda: self.move_hole(1)),
            ("删除", self.delete_hole),
        ]:
            rl.addWidget(QPushButton(text, clicked=fn))
        mid.add(row)
        self.step_tree = QTreeWidget()
        self.step_tree.setHeaderHidden(True)
        self.step_tree.itemSelectionChanged.connect(self.on_step_selected)
        mid.add(self.step_tree)
        mid.add(
            label(
                "保护步骤不可删除；运动到位、文件核验和异常记录由引擎执行。",
                wrap=True,
                role="muted",
            )
        )
        split.addWidget(mid)
        right = Card("步骤参数")
        right.setMinimumWidth(360)
        self.step_title = label("请选择步骤")
        self.step_title.setStyleSheet("font-size:17px;font-weight:600")
        right.add(self.step_title)
        self.tabs = QTabWidget()
        self.form_host = QWidget()
        self.form_host.setStyleSheet("background:white")
        self.form = QFormLayout(self.form_host)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form_host)
        self.tabs.addTab(scroll, "参数设置")
        self.conditions = QPlainTextEdit()
        self.conditions.setReadOnly(True)
        self.tabs.addTab(self.conditions, "前置条件")
        self.tests = QPlainTextEdit()
        self.tests.setReadOnly(True)
        self.tabs.addTab(self.tests, "验证与测试记录")
        right.add(self.tabs)
        self.validation_hint = label("草稿需要检查和现场验证后才能放行。", wrap=True)
        right.add(self.validation_hint)
        split.addWidget(right)
        split.setSizes([230, 320, 550])
        bar = QWidget()
        bl = hbox(bar, margins=0)
        self.save_state = label("")
        bl.addWidget(self.save_state, 1)
        self.check_button = QPushButton("检查配方", clicked=self.check_recipe)
        self.test_step_button = QPushButton("测试选中步骤", clicked=self.test_step)
        self.simulate_button = QPushButton("模拟运行", clicked=self.simulate)
        self.trial_button = QPushButton("现场工程试运行", clicked=self.field_trial)
        self.release_button = QPushButton("验证与放行…", clicked=self.release_recipe)
        self.release_button.setObjectName("primary")
        for c in [
            self.check_button,
            self.test_step_button,
            self.simulate_button,
            self.trial_button,
            self.release_button,
        ]:
            bl.addWidget(c)
        lay.addWidget(bar)
        self.release_hint = label("", wrap=True, role="muted")
        lay.addWidget(self.release_hint)

    def on_show(self):
        note = self.change_note.text() if self._dirty else None
        self.refresh_tree()
        self.refresh_all()
        if note is not None:
            self.change_note.setText(note)

    def _operator(self):
        return self.app.operator_edit.text().strip()

    def _read_only(self):
        v = (
            self.library.get(self.current_id, self.current_rev)
            if self.current_id
            else None
        )
        return not v or v["status"] != DRAFT

    def mark_dirty(self, *_):
        if not self._loading:
            self._dirty = True
            self.save_state.setText("有未保存修改")
            self.release_button.setEnabled(False)

    def _set(self, container, key, text):
        container[key] = text
        self.mark_dirty()

    def _entry(self, title, container, key, default=""):
        value = str(container.get(key, default))
        if key == "experiment" or key.endswith("_experiment"):
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItems(list(dict.fromkeys([value] + self.experiment_names)))
            combo.setCurrentText(value)
            combo.setEnabled(not self._read_only())
            e = combo.lineEdit()
            self.form.addRow(title, combo)
        else:
            e = QLineEdit(value)
            e.setReadOnly(self._read_only())
            self.form.addRow(title, e)
        self.param_edits[key] = e
        e.textChanged.connect(
            lambda t: (
                self._set(container, key, t)
                if not self._loading and not self._read_only()
                else None
            )
        )
        return e

    def _collect_body(self):
        return deepcopy(self._body)

    def refresh(self):
        self.refresh_tree()
        self.refresh_all()

    def refresh_tree(self, *_):
        self._loading = True
        self.recipe_tree.clear()
        first = None
        for r in self.library.recipes():
            if self.search.text().lower() not in r["name"].lower():
                continue
            parent = QTreeWidgetItem([r["name"]])
            parent.setFlags(parent.flags() & ~Qt.ItemIsSelectable)
            self.recipe_tree.addTopLevelItem(parent)
            for v in reversed(r["revisions"]):
                c = QTreeWidgetItem(
                    [
                        f"Rev.{v['revision']:03d} · {STATUS_NAMES[v['status']]}"
                        + (" · 演示" if v["body"].get("example") else "")
                    ]
                )
                c.setData(0, Qt.UserRole, (r["recipe_id"], v["revision"]))
                parent.addChild(c)
                if first is None:
                    first = c
                if (r["recipe_id"], v["revision"]) == (
                    self.current_id,
                    self.current_rev,
                ):
                    self.recipe_tree.setCurrentItem(c)
            parent.setExpanded(True)
        self._loading = False
        if not self.current_id and first:
            self.recipe_tree.setCurrentItem(first)

    def _resolve_dirty(self):
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self,
            "未保存修改",
            "保存当前草稿后切换？",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            return self.save_new_version()
        self._dirty = False
        return True

    def on_version_selected(self):
        if self._loading:
            return
        item = self.recipe_tree.currentItem()
        value = item.data(0, Qt.UserRole) if item else None
        if not value or value == (self.current_id, self.current_rev):
            return
        if not self._resolve_dirty():
            self.refresh_tree()
            return
        self.current_id, self.current_rev = value
        self._body = None
        self._selected_step_id = None
        self.refresh_all()

    def refresh_all(self):
        if not self.current_id:
            return
        v = self.library.get(self.current_id, self.current_rev)
        if self._body is None:
            self._body = deepcopy(v["body"])
        self._loading = True
        self.meta_name.setText(self.library.find(self.current_id)["name"])
        self.meta_badge.setText(
            f"Rev.{self.current_rev:03d} · {STATUS_NAMES[v['status']]}"
        )
        self.meta_parent.setText(
            "演示配置，禁止用于现场"
            if self._body.get("example")
            else "设备绝对坐标 · 装夹基准须核对"
        )
        self.info_status.setText(STATUS_NAMES[v["status"]])
        self.info_author.setText("修改人：" + v.get("created_by", ""))
        self.change_note.setText(v.get("change_note", ""))
        self.change_note.setReadOnly(self._read_only())
        self.template.setCurrentText(self._body.get("template", ""))
        self.template.setEnabled(not self._read_only())
        self._loading = False
        self.refresh_step_tree()
        self.refresh_params()
        self.update_enabled(v)

    def refresh_step_tree(self):
        self._loading = True
        self.step_tree.clear()
        first = None
        for node in self._body.get("step_tree", []):
            c = QTreeWidgetItem(
                [node["label"] + ("  🔒" if node.get("protected") else "")]
            )
            c.setData(0, Qt.UserRole, (node["step_id"], None))
            self.step_tree.addTopLevelItem(c)
            if first is None:
                first = c
            if node["step_id"] == self._selected_step_id:
                self.step_tree.setCurrentItem(c)
            for child in node.get("children", []):
                cc = QTreeWidgetItem([child["label"]])
                cc.setData(0, Qt.UserRole, (child["step_id"], node["step_id"]))
                c.addChild(cc)
                if child["step_id"] == self._selected_step_id:
                    self.step_tree.setCurrentItem(cc)
            c.setExpanded(True)
        self._loading = False
        if not self._selected_step_id and first:
            self.step_tree.setCurrentItem(first)

    def on_step_selected(self):
        if self._loading:
            return
        item = self.step_tree.currentItem()
        v = item.data(0, Qt.UserRole) if item else None
        self._selected_step_id = v[0] if v else None
        self.refresh_params()

    def _find_step(self, sid, group_id=None):
        for n in self._body.get("step_tree", []):
            if n["step_id"] == sid:
                return n
            for c in n.get("children", []):
                if c["step_id"] == sid:
                    return c

    def refresh_params(self):
        self._loading = True
        self.param_edits = {}
        while self.form.rowCount():
            self.form.removeRow(0)
        node = self._find_step(self._selected_step_id)
        if not node:
            self._loading = False
            return
        self.step_title.setText(node["label"])
        kind = node["kind"]
        common = self._body["common_params"]
        self.form.addRow(
            label("设备绝对坐标 · XY/Z：µm · Rx/Ry：µrad", wrap=True, role="muted")
        )
        if kind in ("common", "tilt", "hole"):
            self.form.addRow(
                QPushButton(
                    "读取可用 ZEN 实验（只读）", clicked=self.refresh_experiments
                )
            )
        if kind == "common":
            for k, t in COMMON.items():
                self._entry(t, common, k)
            combo = QComboBox()
            combo.addItems(["共聚焦 · RunExperiment", "相机 · RunSnap"])
            combo.setCurrentIndex(
                1 if common.get("capture_kind") == "camera_snap" else 0
            )
            combo.setEnabled(not self._read_only())
            combo.currentIndexChanged.connect(
                lambda v: self._set(
                    common, "capture_kind", "camera_snap" if v else "confocal"
                )
            )
            self.form.addRow("采集方式", combo)
        elif kind == "tilt":
            tilt = self._body.setdefault("tilt", {})
            for k, t in [
                ("experiment", "测角实验"),
                ("z_um", "预期焦面 Z · µm"),
                ("af_half_um", "AF 半范围 · µm"),
                ("af_timeout_s", "AF 超时 · s"),
                ("arrival_timeout_s", "移动超时 · s"),
            ]:
                self._entry(t, tilt, k)
            self.form.addRow(
                label("高度方式：多点 AF 获取 Z；所有指定点必须成功。", wrap=True)
            )
            table = QTableWidget(len(tilt.get("points_um", [])), 3)
            table.setHorizontalHeaderLabels(["X · µm", "Y · µm", "预期 Z（可空）· µm"])
            table.setMinimumHeight(190)
            for r, row in enumerate(tilt.get("points_um", [])):
                for c, val in enumerate(row):
                    table.setItem(r, c, QTableWidgetItem(str(val)))
            table.setEnabled(not self._read_only())

            def sync():
                tilt["points_um"] = [
                    [
                        table.item(r, c).text() if table.item(r, c) else ""
                        for c in range(3)
                    ]
                    for r in range(table.rowCount())
                ]
                self.mark_dirty()

            table.itemChanged.connect(lambda *_: sync())
            self.point_table = table
            self.form.addRow(table)
            row = QWidget()
            rl = hbox(row, margins=0)

            def add():
                table.insertRow(table.rowCount())
                sync()

            def remove():
                if table.currentRow() >= 0:
                    table.removeRow(table.currentRow())
                    sync()

            rl.addWidget(QPushButton("＋测角点", clicked=add))
            rl.addWidget(QPushButton("删除所选点", clicked=remove))
            rl.addWidget(QPushButton("读取当前 XY/Z", clicked=self.teach_tilt))
            row.setEnabled(not self._read_only())
            self.form.addRow(row)
        elif kind == "gate":
            for k, t in [
                ("max_rx_urad", "最大 |Rx| · µrad"),
                ("max_ry_urad", "最大 |Ry| · µrad"),
                ("max_rms_um", "最大 RMS · µm"),
            ]:
                self._entry(t, common, k)
            self.form.addRow(
                label("不合格：保存角度结果，停止后续孔位采集。", wrap=True)
            )
        elif kind in ("hole", "transit"):
            params = node.setdefault("params", {})
            for k, t in HOLE.items():
                if kind == "transit" and k not in ("x_um", "y_um"):
                    continue
                self._entry(t, params, k, 5 if k.endswith("half_um") else "")
            if kind == "hole":
                af = QCheckBox("启用局部 AF")
                af.setChecked(params.get("use_autofocus", False))
                af.setEnabled(not self._read_only())
                af.toggled.connect(lambda v: self._set(params, "use_autofocus", v))
                self.form.addRow(af)
                self.form.addRow(
                    label(
                        "同一孔位上下层之间不移动 XY。实验留空时继承公共设置。",
                        wrap=True,
                    )
                )
            self.form.addRow(QPushButton("读取当前位置示教…", clicked=self.teach))
        else:
            self.form.addRow(
                label(
                    {
                        "safe_transfer": "每次 XY 转场前先到安全 Z，再确认 XY 到位。",
                        "return_z": "正常结束返回安全 Z；异常时不自动移动。",
                        "archive": "自动保存本次配方版本、参数快照、角度、图片和诊断。",
                        "group": "按子节点顺序逐孔采集，可添加孔位或中间转场点。",
                    }.get(kind, ""),
                    wrap=True,
                )
            )
            self.form.addRow(
                "安全 Z · µm",
                label(str(common.get("safe_z_um", "待示教")) + " · 继承公共参数"),
            )
        self.conditions.setPlainText(
            "前置条件\n• 设备已连接且无待恢复动作\n• 本次参数检查通过\n• 运动路径及实验行为已现场核对\n\n软件 Z 范围不是硬件限位，不能约束 ZEN 内部运动。\n\n实验依赖：\n"
            + json.dumps(
                self._body.get("external_dependencies", []),
                ensure_ascii=False,
                indent=2,
            )
        )
        v = self.library.get(self.current_id, self.current_rev)
        self.tests.setPlainText(
            json.dumps(
                {"checks": v.get("checks"), "validation": v.get("validation")},
                ensure_ascii=False,
                indent=2,
            )
        )
        self._loading = False

    def update_enabled(self, v):
        self.check_button.setEnabled(v["status"] == DRAFT)
        self.save_button.setEnabled(v["status"] == DRAFT)
        self.save_state.setText(
            "有未保存修改"
            if self._dirty
            else "草稿已保存" if v["status"] == DRAFT else "已放行 / 归档版本只读"
        )
        valid = v.get("checks", {})
        valid = bool(valid and valid.get("result") == "passed")
        self.release_button.setEnabled(
            not self._dirty
            and v["status"] == DRAFT
            and valid
            and v.get("validation", {}).get("state") == "field_trial_passed"
        )
        self.release_hint.setText(
            "现场放行须关联本修订成功的真实设备试运行；模拟验证不会放行现场配方。"
        )

    def change_template(self, text):
        if self._loading or not self._body or self._read_only():
            return
        self._body["template"] = text
        nodes = self._body["step_tree"]
        has = any(n["kind"] == "tilt" for n in nodes)
        if text == "仅上下孔采图":
            self._body["step_tree"] = [
                n for n in nodes if n["kind"] not in ("tilt", "gate")
            ]
        elif not has:
            self._body["step_tree"] = (
                nodes[:1] + deepcopy(default_body()["step_tree"][1:3]) + nodes[1:]
            )
        self.mark_dirty()
        self.refresh_step_tree()
        self.refresh_params()

    def new_recipe(self):
        if not self._resolve_dirty():
            return
        name, ok = QInputDialog.getText(self, "新建配方", "产品 / 配方名称")
        if ok and name.strip():
            self.current_id, self.current_rev = self.library.create(
                name.strip(), self._operator()
            )
            self._body = None
            self.refresh()

    def seed_demo(self):
        if not self._resolve_dirty():
            return
        self.current_id, self.current_rev = self.library.create(
            "演示 · 测角与上下孔",
            "演示",
            body=default_body("演示 · 测角与上下孔", example=True),
        )
        self._body = None
        self.refresh()

    def copy_draft(self):
        if not self.current_id:
            return
        self.current_rev = self.library.new_revision(
            self.current_id,
            self._collect_body(),
            by=self._operator(),
            change_note="复制为草稿",
            base_revision=self.current_rev,
        )
        self._body = None
        self._dirty = False
        self.refresh()

    def save_new_version(self):
        if not self.current_id:
            return False
        if self._read_only():
            self.copy_draft()
            return True
        try:
            body = self._collect_body()
            # Drafts can be incomplete, but malformed numeric edits cannot silently keep an old value.
            from zen_recipe_compile import number

            def clean(value, key=""):
                if isinstance(value, dict):
                    return {k: clean(v, k) for k, v in value.items()}
                if isinstance(value, list):
                    return [clean(v, key) for v in value]
                if key.endswith(("_um", "_s", "_urad")) and value != "":
                    return number(value, key)
                return value

            body = clean(body)
            self.library.save_draft(
                self.current_id,
                self.current_rev,
                body,
                by=self._operator(),
                note=self.change_note.text(),
            )
            self._body = body
            self._dirty = False
            self.refresh_all()
            return True
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return False

    def check_recipe(self):
        if self._dirty and not self.save_new_version():
            return
        try:
            report = self.library.check(self.current_id, self.current_rev)
            self.refresh_all()
            self.tabs.setCurrentIndex(2)
            self.validation_hint.setText(
                "参数检查通过，仍需现场验证。"
                if report["result"] == "passed"
                else report["items"][0]["detail"]
            )
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "检查配方", str(e))

    def compare_versions(self):
        r = self.library.find(self.current_id) if self.current_id else None
        if not r:
            return
        others = [v for v in r["revisions"] if v["revision"] != self.current_rev]
        if not others:
            QMessageBox.information(self, "版本比较", "暂无其他版本")
            return
        other = max(others, key=lambda v: v["revision"])
        report = self.library.compare(
            self.current_id, other["revision"], self.current_rev
        )
        d = QDialog(self)
        d.setWindowTitle("版本比较")
        d.resize(720, 520)
        l = vbox(d)
        text = QPlainTextEdit(json.dumps(report, ensure_ascii=False, indent=2))
        text.setReadOnly(True)
        l.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(d.reject)
        l.addWidget(buttons)
        d.exec()

    def add_step(self):
        if self._read_only():
            return
        choice, ok = QInputDialog.getItem(
            self, "添加步骤", "选择步骤库动作", ["上下孔采图", "中间转场点"], 0, False
        )
        if ok:
            self.add_hole("transit" if choice == "中间转场点" else "hole")

    def add_hole(self, kind="hole"):
        if self._read_only():
            return
        g = next(n for n in self._body["step_tree"] if n["kind"] == "group")
        used = {c["step_id"] for c in g.get("children", [])}
        i = 1
        prefix = "V" if kind == "transit" else "H"
        while f"{prefix}{i:03d}" in used:
            i += 1
        sid = f"{prefix}{i:03d}"
        g.setdefault("children", []).append(
            {
                "step_id": sid,
                "kind": kind,
                "label": sid + (" 中间转场" if kind == "transit" else " 上下孔采图"),
                "protected": False,
                "params": {"x_um": "", "y_um": "", "upper_z_um": "", "lower_z_um": ""},
            }
        )
        self._selected_step_id = sid
        self.mark_dirty()
        self.refresh_step_tree()
        self.refresh_params()

    def delete_hole(self):
        if self._read_only():
            return
        n = self._find_step(self._selected_step_id)
        if not n or n["kind"] not in ("hole", "transit"):
            return
        g = next(n for n in self._body["step_tree"] if n["kind"] == "group")
        g["children"].remove(n)
        self._selected_step_id = None
        self.mark_dirty()
        self.refresh_step_tree()
        self.refresh_params()

    def move_hole(self, delta):
        if self._read_only():
            return
        g = next(n for n in self._body["step_tree"] if n["kind"] == "group")
        cs = g["children"]
        i = next(
            (i for i, n in enumerate(cs) if n["step_id"] == self._selected_step_id), -1
        )
        if i >= 0 and 0 <= i + delta < len(cs):
            cs[i], cs[i + delta] = cs[i + delta], cs[i]
            self.mark_dirty()
            self.refresh_step_tree()

    def _run(self, mode, step=None):
        if self._dirty and not self.save_new_version():
            return
        if not self.current_id:
            return
        center = self.app.pages["sequence"]
        center.select_recipe(self.current_id, self.current_rev, mode=mode, step=step)
        self.app.navigate("sequence")

    def simulate(self):
        if self.app.workflow and self.app.workflow.busy:
            return
        if self._dirty and not self.save_new_version():
            return
        self.app.connect_demo("sequence")
        self._run("simulation")

    def field_trial(self):
        if self.app.workflow and self.app.workflow.busy:
            return
        self._run("engineering")

    def test_step(self):
        if self.app.workflow and self.app.workflow.busy:
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
        busy = bool(self.app.workflow and self.app.workflow.busy)
        for button in (self.simulate_button, self.trial_button, self.test_step_button):
            button.setEnabled(not busy and self.current_id is not None)

    def release_recipe(self):
        if self._dirty:
            QMessageBox.warning(self, "放行", "请先保存并重新验证")
            return
        if not self._operator():
            QMessageBox.warning(self, "放行", "请填写工程师姓名")
            return
        if (
            QMessageBox.question(
                self,
                "现场放行",
                "已核对本版本现场试运行、运动路径与实验行为，确认放行？",
            )
            != QMessageBox.Yes
        ):
            return
        try:
            self.library.release(
                self.current_id,
                self.current_rev,
                by=self._operator(),
                note=self.change_note.text(),
            )
            self.refresh_all()
        except ValueError as e:
            QMessageBox.warning(self, "放行", str(e))

    def teach_tilt(self):
        w = self.app.workflow
        if self._read_only() or not w or w.busy or not hasattr(self, "point_table"):
            return
        row = self.point_table.currentRow()
        points = self._body.get("tilt", {}).get("points_um", [])
        if not 0 <= row < len(points):
            self.validation_hint.setText("请先选择一个测角点")
            return
        token = (
            self.current_id,
            self.current_rev,
            row,
            json.dumps(points, ensure_ascii=False),
        )
        try:
            w.read_only(
                "recipe_tilt_teach",
                lambda d: {"target": token, "xy": d.read_xy(), "z": d.read_z()},
            )
        except RuntimeError as e:
            self.validation_hint.setText(str(e))

    def teach(self):
        w = self.app.workflow
        n = self._find_step(self._selected_step_id)
        if self._read_only() or not w or w.busy:
            return
        sid = n["step_id"]
        rid = self.current_id
        rev = self.current_rev
        choice, ok = QInputDialog.getItem(
            self, "示教", "读取到哪个参数？", ["XY", "上孔 Z", "下孔 Z"], 0, False
        )
        if ok:
            try:
                w.read_only(
                    "recipe_teach",
                    lambda d: {
                        "target": (rid, rev, sid),
                        "part": choice,
                        "xy": d.read_xy(),
                        "z": d.read_z(),
                    },
                )
            except RuntimeError as e:
                self.validation_hint.setText(str(e))

    def refresh_experiments(self):
        w = self.app.workflow
        if not w or w.busy:
            return
        try:
            w.read_only(
                "recipe_experiments",
                lambda d: d.call_hardware_operation("list_experiments", {}),
            )
        except (ValueError, RuntimeError) as e:
            self.validation_hint.setText(str(e))

    def on_rpc_error(self, tag, error):
        if tag in ("recipe_teach", "recipe_experiments", "recipe_tilt_teach"):
            self.validation_hint.setText(str(error))

    def on_rpc_result(self, tag, value):
        if tag == "recipe_tilt_teach":
            rid, rev, row, fingerprint = value["target"]
            points = self._body.get("tilt", {}).get("points_um", [])
            if (
                (rid, rev) != (self.current_id, self.current_rev)
                or self._read_only()
                or json.dumps(points, ensure_ascii=False) != fingerprint
            ):
                return
            points[row] = [value["xy"][0] * 1e6, value["xy"][1] * 1e6, value["z"] * 1e6]
            self.mark_dirty()
            self.refresh_params()
            return
        if tag == "recipe_experiments":
            names = []

            def collect(v):
                if isinstance(v, dict):
                    for key, item in v.items():
                        if key in ("name", "experiment_name") and isinstance(item, str):
                            names.append(item)
                        elif (
                            key in ("experiments", "experiment_names")
                            and isinstance(item, list)
                            and all(isinstance(x, str) for x in item)
                        ):
                            names.extend(item)
                        else:
                            collect(item)
                elif isinstance(v, list):
                    for item in v:
                        collect(item)

            collect(value)
            self.experiment_names = sorted(set(names))
            self.refresh_params()
            self.validation_hint.setText(
                f"已读取 {len(self.experiment_names)} 个实验名称；内容指纹在执行时核对。"
            )
            return
        if (
            tag != "recipe_teach"
            or tuple(value["target"][:2]) != (self.current_id, self.current_rev)
            or self._read_only()
        ):
            return
        n = self._find_step(value["target"][2])
        p = n["params"]
        if value["part"] == "XY":
            p.update(x_um=value["xy"][0] * 1e6, y_um=value["xy"][1] * 1e6)
        else:
            p["upper_z_um" if value["part"] == "上孔 Z" else "lower_z_um"] = (
                value["z"] * 1e6
            )
        self.mark_dirty()
        self.refresh_params()

    def import_legacy(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入旧 Sequence 配置", "", "JSON (*.json)"
        )
        if not path or not self._resolve_dirty():
            return
        try:
            if Path(path).stat().st_size > 1024 * 1024:
                raise ValueError("配置文件超过 1 MiB")
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if (
                not isinstance(raw, dict)
                or raw.get("kind") != "sequence_ui"
                or raw.get("schema_version") != 1
            ):
                raise ValueError("需要旧版 Sequence UI 配置")
            draft = raw.get("draft", {})
            if draft.get("entry_unit") != "um":
                raise ValueError("旧界面参数单位必须明确为 µm")
            if draft.get("tilt") and draft.get("tilt_coordinate_unit") != "m":
                raise ValueError("测角快照单位必须明确为 m")
            fields = draft.get("fields", {})
            body = default_body(fields.get("name", "导入配方"))
            body["example"] = bool(draft.get("simulated_configuration"))
            for key in body["common_params"]:
                if key in fields:
                    body["common_params"][key] = fields[key]
            body["common_params"]["capture_kind"] = draft.get(
                "capture_kind", "confocal"
            )
            body["common_params"]["use_autofocus"] = draft.get("use_autofocus", False)
            for key in ("max_abs_angle_urad", "max_rx_urad", "max_ry_urad"):
                body["common_params"][key] = fields.get("max_angle_urad", "")
            old = draft.get("tilt") or {}
            if "tolerance_m" in old:
                body["common_params"]["xy_tolerance_um"] = old["tolerance_m"] * 1e6
            body["tilt"].update(
                experiment=old.get("experiment", ""),
                points_um=[[x * 1e6, y * 1e6] for x, y in old.get("targets", [])],
                z_um=fields.get("tilt_z_um", ""),
                af_timeout_s=old.get("af_timeout", 30),
                arrival_timeout_s=old.get("arrival_timeout", 15),
            )
            g = next(n for n in body["step_tree"] if n["kind"] == "group")
            g["children"] = []
            for row in draft.get("rows", []):
                name, kind, x, y, up, low = row
                g["children"].append(
                    {
                        "step_id": name,
                        "kind": "transit" if kind == "转场" else "hole",
                        "label": name,
                        "params": dict(x_um=x, y_um=y, upper_z_um=up, lower_z_um=low),
                    }
                )
            self.current_id, self.current_rev = self.library.create(
                body["product_family"], self._operator(), body=body
            )
            self._body = None
            self.refresh()
            self.validation_hint.setText(
                "已导入为待验证草稿；旧版未定义测角 AF 半范围，请示教核对。其他未知扩展字段不执行，请与原文件对照。"
            )
        except (ValueError, KeyError, TypeError, OSError) as e:
            QMessageBox.warning(self, "导入失败", str(e))
