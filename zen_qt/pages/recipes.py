"""Recipe management: versions, constrained step tree and step parameters.

The page never issues a device command. Every badge, check result and approval
comes from RecipeLibrary, which persists an immutable revision with a content
hash. Released revisions are read-only here; production runs are started in
the run centre and freeze their own snapshot.
"""
from copy import deepcopy

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from zen_recipe_store import (ARCHIVED, DRAFT, RELEASED, STATUS_NAMES,
                              RecipeLibrary, default_body)
from .. import theme
from ..widgets import Card, hbox, label, vbox

STATUS_STYLE = {DRAFT: "warn", RELEASED: "ok", ARCHIVED: "mute"}
PROTECTED_KINDS = {"common", "tilt", "gate", "safe_transfer", "group",
                   "return_z", "archive"}


def _badge(text, kind="mute"):
    bg, fg, line = theme.PILL_STYLES[kind]
    widget = QLabel(text)
    widget.setStyleSheet(
        f"background:{bg}; color:{fg}; border:1px solid {line};"
        " border-radius:9px; padding:2px 9px; font-size:11px;")
    return widget


def _head(text):
    widget = label(text)
    widget.setStyleSheet(f"font-size:13px; font-weight:600; color:{theme.INK};")
    return widget


def _hint(text, kind="info"):
    widget = QLabel(text)
    widget.setWordWrap(True)
    colours = {"info": (theme.ACCENT_SOFT, theme.ACCENT_INK, "#c5d9f5"),
               "warn": (theme.WARN_SOFT, theme.WARN, theme.WARN_LINE)}
    bg, fg, line = colours[kind]
    widget.setStyleSheet(
        f"background:{bg}; color:{fg}; border:1px solid {line};"
        " border-radius:8px; padding:8px 10px; font-size:12px;")
    return widget


def _scroll(content):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.NoFrame)
    area.setWidget(content)
    return area


class RecipesPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.library = RecipeLibrary(app.data_root / "recipes.json")
        self._seed()
        self.current_id = None
        self.current_rev = None
        self._body = None
        self._dirty = False
        self._loading = False
        self._selected_step_id = None
        self.param_edits = {}
        self._build()
        self.refresh()

    # ------------------------------------------------------------------
    def _seed(self):
        """First run: create an explicitly-labelled demo library through the
        real API. Nothing here is forged as 'already validated'."""
        if self.library.recipes():
            return
        a, r1 = self.library.create("产品 A · 测角与上下孔", "示例数据")
        body = deepcopy(self.library.get(a, r1)["body"])
        body["common_params"]["upper_experiment"] = "Upper_mark"
        body["common_params"]["lower_experiment"] = "Lower_mark"
        body["external_dependencies"][0]["zen_experiment_name"] = "Upper_mark"
        r2 = self.library.new_revision(a, body, by="示例数据",
                                       change_note="填写上下孔实验名")
        self.library.archive(a, r1, by="示例数据")
        body2 = deepcopy(body)
        body2["change_note"] = "调整 H01 上下层焦面"
        r3 = self.library.new_revision(a, body2, by="示例数据",
                                       change_note="调整 H01 上下层焦面")
        self.library.archive(a, r2, by="示例数据")
        b, rb = self.library.create("产品 B · 仅上下孔采图", "示例数据",
                                    body=default_body("产品 B"))
        bb = deepcopy(self.library.get(b, rb)["body"])
        bb["common_params"]["upper_experiment"] = "Upper_mark"
        bb["common_params"]["lower_experiment"] = "Lower_mark"
        bb["external_dependencies"][0]["zen_experiment_name"] = "Upper_mark"
        rb2 = self.library.new_revision(b, bb, by="示例数据",
                                        change_note="填写实验名")
        self.library.archive(b, rb, by="示例数据")
        self.library.check(b, rb2)
        self.library.release(b, rb2, by="示例数据",
                             note="示例放行：参数未经实机验证")
        self.library.create("联调测试配方", "示例数据")

    # ------------------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 14, 20, 10)
        outer.setSpacing(10)

        title = QWidget()
        tl = hbox(title, spacing=10, margins=0)
        heading = label("配方管理")
        heading.setStyleSheet("font-size:19px; font-weight:700;")
        tl.addWidget(heading)
        tl.addWidget(label("选择配方  →  编排流程  →  设置步骤参数", role="muted"))
        tl.addStretch(1)
        tl.addWidget(_badge("设计预览", "accent"))
        outer.addWidget(title)

        meta = QWidget()
        ml = hbox(meta, spacing=8, margins=0)
        self.meta_name = _head("—")
        self.meta_badge = _badge("—")
        self.meta_parent = label("", role="muted")
        ml.addWidget(self.meta_name)
        ml.addWidget(self.meta_badge)
        ml.addWidget(self.meta_parent)
        ml.addStretch(1)
        self.compare_button = QPushButton("版本比较", clicked=self.compare_versions)
        self.save_button = QPushButton("保存新版本", clicked=self.save_new_version)
        self.save_button.setObjectName("primary")
        ml.addWidget(self.compare_button)
        ml.addWidget(self.save_button)
        outer.addWidget(meta)

        columns = QWidget()
        cl = QHBoxLayout(columns)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(10)
        cl.addWidget(self._build_left(), 0)
        cl.addWidget(self._build_middle(), 0)
        cl.addWidget(self._build_right(), 1)
        outer.addWidget(columns, 1)
        outer.addWidget(self._build_footer())

    def _build_left(self):
        panel = Card()
        panel.setFixedWidth(232)
        panel.add(_head("配方与版本"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索产品或配方")
        self.search.textChanged.connect(self.refresh_tree)
        panel.add(self.search)
        row = QWidget()
        rl = hbox(row, spacing=6, margins=0)
        rl.addWidget(QPushButton("＋ 新建", clicked=self.new_recipe))
        rl.addWidget(QPushButton("复制为草稿", clicked=self.copy_draft))
        panel.add(row)
        self.recipe_tree = QTreeWidget()
        self.recipe_tree.setHeaderHidden(True)
        self.recipe_tree.setMinimumHeight(220)
        self.recipe_tree.itemSelectionChanged.connect(self.on_version_selected)
        panel._lay.addWidget(self.recipe_tree, 1)
        card = Card()
        card.add(_head("当前版本"))
        grid = QWidget()
        gl = QGridLayout(grid)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setVerticalSpacing(6)
        gl.addWidget(label("状态", role="muted"), 0, 0)
        self.info_status = _badge("—")
        gl.addWidget(self.info_status, 0, 1)
        gl.addWidget(label("修改人", role="muted"), 1, 0)
        self.info_author = label("—", role="muted")
        gl.addWidget(self.info_author, 1, 1)
        card.add(grid)
        card.add(label("变更说明", role="muted"))
        self.change_note = QLineEdit()
        self.change_note.setPlaceholderText("保存新版本时填写")
        card.add(self.change_note)
        card.add(_hint("已验证 / 已归档版本只读；修改会生成新的草稿版本。"))
        panel.add(card)
        return panel

    def _build_middle(self):
        panel = Card()
        panel.setFixedWidth(320)
        panel.add(_head("执行流程"))
        row = QWidget()
        rl = hbox(row, spacing=6, margins=0)
        self.template = QComboBox()
        self.template.addItem("测角 → 判定 → 上下孔采图")
        self.template.setEnabled(False)
        rl.addWidget(self.template, 1)
        panel.add(row)
        buttons = QWidget()
        bl = hbox(buttons, spacing=5, margins=0)
        for text, fn in (("＋ 添加步骤", self.add_hole), ("上移", lambda: self.move_hole(-1)),
                         ("下移", lambda: self.move_hole(1)), ("删除", self.delete_hole)):
            bl.addWidget(QPushButton(text, clicked=fn))
        panel.add(buttons)
        self.step_tree = QTreeWidget()
        self.step_tree.setHeaderHidden(True)
        self.step_tree.setMinimumHeight(260)
        self.step_tree.itemSelectionChanged.connect(self.on_step_selected)
        panel._lay.addWidget(self.step_tree, 1)
        panel.add(_hint("保护步骤不可删除；运动到位检查、图像核验由程序执行。"))
        self.flow_summary = label("", role="muted", wrap=True)
        panel.add(self.flow_summary)
        return panel

    def _build_right(self):
        panel = Card()
        panel.add(_head("步骤参数"))
        self.step_crumb = label("", role="muted")
        panel.add(self.step_crumb)
        self.step_title = _head("—")
        panel.add(self.step_title)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_params_tab(), "参数设置")
        self.tabs.addTab(self._build_conditions_tab(), "前置条件")
        self.tabs.addTab(self._build_tests_tab(), "测试记录")
        panel._lay.addWidget(self.tabs, 1)
        self.validation_hint = _hint("本次变更尚未验证｜待验证：孔位焦面与采图实验", "warn")
        panel.add(self.validation_hint)
        return panel

    def _build_params_tab(self):
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(8)
        coord = QWidget()
        ch = hbox(coord, spacing=6, margins=0)
        ch.addStretch(1)
        self.coord_label = label("坐标系：样品坐标 · 基准 501", role="muted")
        ch.addWidget(self.coord_label)
        lay.addWidget(coord)

        coords = Card()
        coords.add(_head("孔位坐标"))
        self.coord_grid = QGridLayout()
        self.coord_grid.setContentsMargins(0, 0, 0, 0)
        row = QWidget()
        row.setLayout(self.coord_grid)
        self.coord_grid.addWidget(label("参数", role="muted"), 0, 0)
        self.coord_grid.addWidget(label("值", role="muted"), 0, 1)
        self.coord_grid.addWidget(label("单位", role="muted"), 0, 2)
        coords.add(row)
        lay.addWidget(coords)

        layers = Card()
        layers.add(_head("上下层采集"))
        grid = QWidget()
        gl = QGridLayout(grid)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setHorizontalSpacing(8)
        gl.setVerticalSpacing(6)
        for col, text in enumerate(("参数", "上孔", "下孔")):
            gl.addWidget(label(text, role="muted"), 0, col)
        self.layer_grid = gl
        layers.add(grid)
        lay.addWidget(layers)

        motion = Card()
        motion.add(_head("运动与超时"))
        self.motion_grid = QGridLayout()
        self.motion_grid.setContentsMargins(0, 0, 0, 0)
        holder = QWidget()
        holder.setLayout(self.motion_grid)
        motion.add(holder)
        lay.addWidget(motion)
        lay.addWidget(_hint("同一孔位上下层采图之间不移动 XY。"))
        lay.addStretch(1)
        return _scroll(content)

    def _build_conditions_tab(self):
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(8)
        lay.addWidget(_head("外部依赖与不可验证假设"))
        self.conditions = QPlainTextEdit()
        self.conditions.setReadOnly(True)
        lay.addWidget(self.conditions, 1)
        lay.addWidget(_hint("ZEN 实验按名称引用；未采集实验 XML 指纹时，"
                            "这些内容只能由操作员在启动前逐项确认。", "warn"))
        return content

    def _build_tests_tab(self):
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(8)
        lay.addWidget(_head("检查与测试记录"))
        self.test_report = QPlainTextEdit()
        self.test_report.setReadOnly(True)
        lay.addWidget(self.test_report, 1)
        return content

    def _build_footer(self):
        footer = QWidget()
        fl = hbox(footer, spacing=8, margins=0)
        self.save_state = label("草稿已保存 · 生产任务使用独立快照")
        self.save_state.setStyleSheet(f"color:{theme.OK}; font-size:12px;")
        fl.addWidget(self.save_state)
        fl.addStretch(1)
        self.check_button = QPushButton("检查配方", clicked=self.check_recipe)
        self.test_step_button = QPushButton("测试选中步骤")
        self.simulate_button = QPushButton("模拟运行")
        self.release_button = QPushButton("验证与放行…", clicked=self.release_recipe)
        self.release_button.setObjectName("primary")
        for button in (self.test_step_button, self.simulate_button):
            button.setEnabled(False)
            button.setToolTip("需连接设备后到运行中心执行；配方管理页不启动设备")
        for button in (self.check_button, self.test_step_button,
                       self.simulate_button, self.release_button):
            fl.addWidget(button)
        self.release_hint = label("完成检查后可放行", role="muted")
        fl.addWidget(self.release_hint)
        bar = QWidget()
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        bl.addWidget(footer)
        bottom = QWidget()
        bh = hbox(bottom, spacing=10, margins=0)
        bh.addWidget(label("验证与测试记录", role="muted"))
        bh.addWidget(label("当前无生产任务运行（本页不启动设备）", role="muted"))
        bh.addStretch(1)
        bl.addWidget(bottom)
        return bar

    # ------------------------------------------------------------------
    def refresh(self):
        self.refresh_tree()
        self.refresh_all()

    def refresh_tree(self):
        self._loading = True
        self.recipe_tree.clear()
        needle = self.search.text().strip().lower()
        first = None
        for recipe in self.library.recipes():
            versions = recipe.get("revisions", [])
            text = " · ".join([recipe.get("name", "")] +
                              [f"Rev.{v['revision']:03d}" for v in versions])
            if needle and needle not in text.lower():
                continue
            parent = QTreeWidgetItem([recipe.get("name", "")])
            parent.setFlags(parent.flags() & ~Qt.ItemIsSelectable)
            self.recipe_tree.addTopLevelItem(parent)
            for version in reversed(versions):
                status = version["status"]
                child = QTreeWidgetItem([f"Rev.{version['revision']:03d}   {STATUS_NAMES[status]}"])
                child.setData(0, Qt.UserRole, (recipe["recipe_id"], version["revision"]))
                if status == RELEASED:
                    child.setForeground(0, QBrush(QColor(theme.OK)))
                elif status == ARCHIVED:
                    child.setForeground(0, QBrush(QColor(theme.MUTED)))
                parent.addChild(child)
                if first is None:
                    first = child
            parent.setExpanded(True)
        self._loading = False
        if self.current_id is None and first is not None:
            self.recipe_tree.setCurrentItem(first)
        elif self.current_id is not None:
            self._reselect_current()
        self.recipe_tree.resizeColumnToContents(0)

    def _reselect_current(self):
        root = self.recipe_tree.invisibleRootItem()
        for i in range(root.childCount()):
            parent = root.child(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.data(0, Qt.UserRole) == (self.current_id, self.current_rev):
                    self.recipe_tree.setCurrentItem(child)
                    return

    def on_version_selected(self):
        if self._loading:
            return
        items = self.recipe_tree.selectedItems()
        if not items:
            return
        value = items[0].data(0, Qt.UserRole)
        if not value:
            return
        self.current_id, self.current_rev = value
        self._body = None
        self._dirty = False
        self._selected_step_id = None
        self.refresh_all()

    def refresh_all(self):
        version = self.library.get(self.current_id, self.current_rev)
        if version is None:
            return
        if self._body is None:
            self._body = deepcopy(version["body"])
        recipe = self.library.find(self.current_id) or {}
        status = version["status"]
        self.meta_name.setText(recipe.get("name", "—"))
        self._replace_badge(self.meta_badge,
                            f"Rev.{self.current_rev:03d} · {STATUS_NAMES[status]}",
                            STATUS_STYLE[status])
        parent = version.get("parent_revision")
        base = self.library.get(self.current_id, parent) if parent else None
        self.meta_parent.setText(
            f"基于 Rev.{parent:03d} {STATUS_NAMES[base['status']]} 版本" if base else "首个版本")
        self._replace_badge(self.info_status, STATUS_NAMES[status], STATUS_STYLE[status])
        self.info_author.setText(version.get("created_by") or "—")
        self._loading = True
        self.change_note.setText(version.get("change_note", ""))
        self._loading = False
        self.coord_label.setText("坐标系：" + self._body.get("coordinate_system", "未设置"))
        self.refresh_step_tree()
        self.refresh_conditions()
        self.refresh_tests(version)
        self.update_enabled(version)

    @staticmethod
    def _replace_badge(old, text, kind):
        bg, fg, line = theme.PILL_STYLES[kind]
        old.setText(text)
        old.setStyleSheet(
            f"background:{bg}; color:{fg}; border:1px solid {line};"
            " border-radius:9px; padding:2px 9px; font-size:11px;")

    # ------------------------------------------------------------------
    def refresh_step_tree(self):
        self._loading = True
        self.step_tree.clear()
        selected = None
        index = 0
        for node in self._body.get("step_tree", ()):
            index += 1
            locked = node.get("protected", node.get("kind") in PROTECTED_KINDS)
            text = f"{index:02d}  {node.get('label', node.get('step_id',''))}"
            if locked:
                text += "  · 锁定"
            item = QTreeWidgetItem([text])
            item.setData(0, Qt.UserRole, (node.get("step_id"), None))
            if locked:
                item.setForeground(0, QBrush(QColor(theme.MUTED)))
            self.step_tree.addTopLevelItem(item)
            if node.get("step_id") == self._selected_step_id:
                selected = item
            for child in node.get("children", ()) or ():
                ctext = "      " + child.get("label", child.get("step_id", ""))
                sub = QTreeWidgetItem([ctext])
                sub.setData(0, Qt.UserRole, (child.get("step_id"), node.get("step_id")))
                item.addChild(sub)
                if child.get("step_id") == self._selected_step_id:
                    selected = sub
            item.setExpanded(True)
        self._loading = False
        if selected is not None:
            self.step_tree.setCurrentItem(selected)
        else:
            self._show_no_step()

    def on_step_selected(self):
        if self._loading:
            return
        items = self.step_tree.selectedItems()
        if not items:
            return
        value = items[0].data(0, Qt.UserRole)
        self._selected_step_id = value[0] if value else None
        self.refresh_params()

    def _find_step(self, step_id, group_id=None):
        for node in self._body.get("step_tree", ()):
            if group_id is None and node.get("step_id") == step_id:
                return node
            for child in node.get("children", ()) or ():
                if child.get("step_id") == step_id:
                    return child
        return None

    def _show_no_step(self):
        self.step_crumb.setText("执行流程")
        self.step_title.setText("请选择流程步骤")

    # ------------------------------------------------------------------
    def _clear_grid(self, grid):
        while grid.count():
            item = grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _edit(self, key, value, *, read_only=False, width=110):
        edit = QLineEdit(str(value))
        edit.setFixedWidth(width)
        edit.setReadOnly(read_only)
        if read_only:
            edit.setStyleSheet(f"background:{theme.INSET}; color:{theme.MUTED};")
        edit.textChanged.connect(self.mark_dirty)
        self.param_edits[key] = edit
        return edit

    def mark_dirty(self, *_):
        if self._loading:
            return
        self._dirty = True
        self.save_state.setText("有未保存修改 · 请保存新版本")
        self.save_state.setStyleSheet(f"color:{theme.WARN}; font-size:12px;")
        self.release_button.setEnabled(False)

    def refresh_params(self):
        self._loading = True
        step = self._find_step(self._selected_step_id)
        self.param_edits = {}
        self._clear_grid(self.coord_grid)
        self._clear_grid(self.layer_grid)
        self._clear_grid(self.motion_grid)
        read_only = self._read_only()
        if step is None:
            self._show_no_step()
            self.tabs.setCurrentIndex(0)
            self._loading = False
            return
        group = next((n for n in self._body.get("step_tree", ())
                      if any(c.get("step_id") == step.get("step_id")
                             for c in n.get("children", ()) or ())), None)
        crumb = (group.get("label", "执行流程") + " / " if group else "配方公共参数 / ")
        self.step_crumb.setText(crumb + step.get("step_id", ""))
        self.step_title.setText(step.get("label", step.get("step_id", "")))

        params = step.get("params", {})
        common = self._body.get("common_params", {})
        self.coord_grid.addWidget(label("参数", role="muted"), 0, 0)
        self.coord_grid.addWidget(label("值", role="muted"), 0, 1)
        self.coord_grid.addWidget(label("单位", role="muted"), 0, 2)
        for row, (key, text) in enumerate((("x_um", "X"), ("y_um", "Y")), 1):
            self.coord_grid.addWidget(label(text, role="muted"), row, 0)
            self.coord_grid.addWidget(self._edit(key, params.get(key, ""),
                                                 read_only=read_only or step.get("kind") != "hole"), row, 1)
            self.coord_grid.addWidget(label("µm", role="muted"), row, 2)

        for col, text in enumerate(("参数", "上孔", "下孔")):
            self.layer_grid.addWidget(label(text, role="muted"), 0, col)
        layer_rows = (("Z", "upper_z_um", "lower_z_um", "µm", True),
                      ("采图实验", "upper_experiment", "lower_experiment", "", False))
        for row, (title, upper, lower, unit, numeric) in enumerate(layer_rows, 1):
            self.layer_grid.addWidget(label(title, role="muted"), row, 0)
            source = params if numeric else common
            self.layer_grid.addWidget(
                self._edit(upper, source.get(upper, ""), read_only=read_only, width=104), row, 1)
            self.layer_grid.addWidget(
                self._edit(lower, source.get(lower, ""), read_only=read_only, width=104), row, 2)
            if unit:
                hint = label(unit, role="muted")
                self.layer_grid.addWidget(hint, row, 3)
        self.layer_grid.addWidget(label("AF（自动对焦）", role="muted"), 3, 0)
        af = "开" if common.get("use_autofocus") else "关"
        for col in (1, 2):
            self.layer_grid.addWidget(_badge(af, "mute"), 3, col)

        self.motion_grid.addWidget(label("安全转场 Z", role="muted"), 0, 0)
        self.motion_grid.addWidget(self._edit("safe_z_um", common.get("safe_z_um", ""),
                                              read_only=True), 0, 1)
        self.motion_grid.addWidget(label("µm  ·  继承公共参数", role="muted"), 0, 2)
        self.motion_grid.addWidget(label("采图超时", role="muted"), 1, 0)
        self.motion_grid.addWidget(self._edit("capture_timeout_s",
                                              common.get("capture_timeout_s", ""),
                                              read_only=read_only), 1, 1)
        self.motion_grid.addWidget(label("s", role="muted"), 1, 2)
        self._loading = False

    def _read_only(self):
        version = self.library.get(self.current_id, self.current_rev)
        return version is None or version["status"] != DRAFT

    def refresh_conditions(self):
        lines = ["外部依赖（ZEN 实验）"]
        for dep in self._body.get("external_dependencies", ()):
            lines.append(f"  {dep.get('logical_name','')}："
                         f"{dep.get('zen_experiment_name') or '未填写'}")
            lines.append(f"    内容指纹：{dep.get('expected_xml_sha256') or '未采集'}")
            lines.append(f"    再验证到期：{dep.get('revalidate_before') or '未设置'}")
            if dep.get("note"):
                lines.append("    " + dep["note"])
        lines.append("")
        lines.append("软件无法验证、须操作员确认的假设")
        for item in self._body.get("unverifiable_assumptions", ()):
            lines.append("  · " + item)
        self.conditions.setPlainText("\n".join(lines))

    def refresh_tests(self, version):
        checks = version.get("checks")
        if not checks:
            self.test_report.setPlainText("尚未执行配方检查。")
            self.validation_hint.setText("本次变更尚未验证｜待验证：孔位焦面与采图实验")
            self.validation_hint.show()
            return
        lines = [f"检查时间：{checks.get('checked_at','')}",
                 f"结果：{'通过' if checks.get('result')=='passed' else '未通过'}",
                 f"内容哈希：{checks.get('content_sha256','')[:16]}…", ""]
        for item in checks.get("items", ()):
            mark = "通过" if item["result"] == "passed" else "未通过"
            lines.append(f"[{mark}] {item['label']}"
                         + (f"  — {item['detail']}" if item.get("detail") else ""))
        for warning in checks.get("warnings", ()):
            lines.append("[提示] " + warning)
        validation = version.get("validation", {})
        if validation.get("state") == "qualified":
            lines += ["", f"放行：{validation.get('approved_by','')} "
                      f"@ {validation.get('approved_at','')}",
                      validation.get("note", "")]
        self.test_report.setPlainText("\n".join(lines))
        passed = checks.get("result") == "passed" and validation.get("state") == "qualified"
        self.validation_hint.setVisible(not passed)
        self.release_hint.setText("检查已通过，可放行" if checks.get("result") == "passed"
                                  else "检查未通过，不能放行")

    def update_enabled(self, version):
        draft = version["status"] == DRAFT
        self.save_button.setEnabled(True)
        self.check_button.setEnabled(draft)
        can_release = (draft and bool(version.get("checks"))
                       and version["checks"].get("result") == "passed")
        self.release_button.setEnabled(can_release)
        self.release_button.setObjectName("primary" if can_release else "")
        self.release_button.style().unpolish(self.release_button)
        self.release_button.style().polish(self.release_button)
        self.change_note.setReadOnly(not draft)
        self.save_state.setText("草稿已保存 · 生产任务使用独立快照" if draft
                                else f"{STATUS_NAMES[version['status']]}版本只读 · 修改请复制为草稿")
        self.save_state.setStyleSheet(
            f"color:{theme.OK if draft else theme.MUTED}; font-size:12px;")

    # ------------------------------------------------------------------
    def _collect_body(self):
        body = deepcopy(self._body)
        common = body.setdefault("common_params", {})
        for key, edit in self.param_edits.items():
            text = edit.text().strip()
            if key in ("upper_experiment", "lower_experiment"):
                common[key] = text
                continue
            try:
                value = float(text)
            except ValueError:
                continue
            if key == "safe_z_um" or key == "capture_timeout_s":
                common[key] = value
            else:
                step = self._find_step(self._selected_step_id)
                if step is not None:
                    step.setdefault("params", {})[key] = value
        return body

    def _operator(self):
        edit = getattr(self.app, "operator_edit", None)
        return edit.text().strip() if edit is not None else ""

    def new_recipe(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "新建配方", "配方名称")
        if not ok or not name.strip():
            return
        self.current_id, self.current_rev = self.library.create(
            name.strip(), self._operator() or "未填写")
        self._body = None
        self.refresh()

    def copy_draft(self):
        version = self.library.get(self.current_id, self.current_rev)
        if version is None:
            return
        body = self._collect_body() if self._dirty else deepcopy(version["body"])
        self.current_rev = self.library.new_revision(
            self.current_id, body, by=self._operator() or "未填写",
            change_note=self.change_note.text().strip() or "复制为草稿",
            base_revision=self.current_rev)
        self._body = None
        self._dirty = False
        self.refresh()

    def save_new_version(self):
        version = self.library.get(self.current_id, self.current_rev)
        if version is None:
            return
        note = self.change_note.text().strip()
        if not note:
            QMessageBox.warning(self, "保存新版本", "请填写变更说明，说明改了什么、为什么改。")
            return
        body = self._collect_body()
        self.current_rev = self.library.new_revision(
            self.current_id, body, by=self._operator() or "未填写",
            change_note=note, base_revision=self.current_rev)
        self._body = None
        self._dirty = False
        self.change_note.clear()
        self.refresh()

    def check_recipe(self):
        try:
            report = self.library.check(self.current_id, self.current_rev)
        except ValueError as error:
            QMessageBox.warning(self, "检查配方", str(error))
            return
        self.refresh_all()
        QMessageBox.information(
            self, "检查配方",
            f"检查{'通过' if report['result']=='passed' else '未通过'}，"
            f"共 {len(report['items'])} 项；详情见「测试记录」标签。")

    def compare_versions(self):
        recipe = self.library.find(self.current_id)
        if not recipe:
            return
        versions = [v for v in recipe["revisions"]
                    if v["revision"] != self.current_rev
                    and v["status"] in (RELEASED, ARCHIVED)]
        if not versions:
            QMessageBox.information(self, "版本比较", "当前配方还没有可比较的历史版本。")
            return
        other = max(versions, key=lambda v: v["revision"])
        report = self.library.compare(self.current_id, other["revision"], self.current_rev)
        dialog = QDialog(self)
        dialog.setWindowTitle("版本比较")
        dialog.setMinimumSize(520, 380)
        lay = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        lines = [f"Rev.{other['revision']:03d}  →  Rev.{self.current_rev:03d}   "
                 f"共 {report['change_count']} 处差异", ""]
        for change in report["changes"]:
            lines.append(f"{change['path']}\n    旧：{change['before']}\n    新：{change['after']}")
        text.setPlainText("\n".join(lines) if report["changes"] else "两个版本内容一致。")
        lay.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        lay.addWidget(buttons)
        dialog.exec()

    def release_recipe(self):
        operator = self._operator()
        if not operator:
            QMessageBox.warning(self, "验证与放行", "请先在左侧会话中填写操作员，再执行放行。")
            return
        try:
            self.library.release(self.current_id, self.current_rev,
                                 by=operator,
                                 note="操作员确认：已核对检查项与外部实验依赖")
        except ValueError as error:
            QMessageBox.warning(self, "验证与放行", str(error))
            return
        self.refresh_all()

    # ------------------------------------------------------------------
    def _selected_hole(self):
        item = self.step_tree.currentItem()
        if item is None:
            return None
        value = item.data(0, Qt.UserRole)
        if not value or not value[1]:
            return None
        step = self._find_step(value[0], value[1])
        return step if step and step.get("kind") == "hole" else None

    def add_hole(self):
        if self._read_only():
            QMessageBox.information(self, "添加步骤", "当前版本只读，请先复制为草稿。")
            return
        group = next((n for n in self._body.get("step_tree", ())
                      if n.get("kind") == "group"), None)
        if group is None:
            return
        children = group.setdefault("children", [])
        used = {c.get("step_id") for c in children}
        index = 1
        while f"H{index:03d}" in used:
            index += 1
        children.append({"step_id": f"H{index:03d}", "kind": "hole",
                         "label": f"H{index:03d} 上下孔采图", "protected": False,
                         "params": {"x_um": 0.0, "y_um": 0.0,
                                    "upper_z_um": 100.0, "lower_z_um": 200.0}})
        self._selected_step_id = f"H{index:03d}"
        self.mark_dirty()
        self.refresh_step_tree()
        self.refresh_params()

    def delete_hole(self):
        hole = self._selected_hole()
        if hole is None:
            QMessageBox.information(self, "删除步骤", "只能删除孔位类步骤；保护步骤不可删除。")
            return
        if self._read_only():
            QMessageBox.information(self, "删除步骤", "当前版本只读，请先复制为草稿。")
            return
        group = next((n for n in self._body.get("step_tree", ())
                      if n.get("kind") == "group"), None)
        group["children"] = [c for c in group.get("children", ())
                             if c.get("step_id") != hole["step_id"]]
        self._selected_step_id = None
        self.mark_dirty()
        self.refresh_step_tree()
        self.refresh_params()

    def move_hole(self, delta):
        hole = self._selected_hole()
        if hole is None or self._read_only():
            return
        group = next((n for n in self._body.get("step_tree", ())
                      if n.get("kind") == "group"), None)
        children = group.get("children", [])
        index = next((i for i, c in enumerate(children)
                      if c.get("step_id") == hole["step_id"]), -1)
        other = index + delta
        if index < 0 or not 0 <= other < len(children):
            return
        children[index], children[other] = children[other], children[index]
        self.mark_dirty()
        self.refresh_step_tree()
