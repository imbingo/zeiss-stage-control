"""Drag/drop order, deletion and reusable operator templates."""

from copy import deepcopy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QTreeWidget,
    QAbstractItemView,
    QMenu,
    QInputDialog,
    QPushButton,
    QMessageBox,
)
from zen_steps import flatten, insert_template
from zen_templates import TemplateStore


class StepTree(QTreeWidget):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def dropEvent(self, event):
        if self.page._read_only():
            event.ignore()
            return
        super().dropEvent(event)
        self.page.accept_tree_order()


class RecipeTools:
    def install_recipe_tools(self):
        old = self.step_tree
        layout = old.parentWidget().layout()
        index = layout.indexOf(old)
        self.step_tree = StepTree(self)
        self.step_tree.setHeaderHidden(True)
        self.step_tree.itemSelectionChanged.connect(self.on_step_selected)
        layout.removeWidget(old)
        old.deleteLater()
        layout.insertWidget(index, self.step_tree, 1)
        self.templates = TemplateStore(self.app.data_root / "step_templates.json")
        button = QPushButton("保存流程为模板…", clicked=self.save_template)
        layout.insertWidget(2, button)
        self.recipe_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.recipe_tree.customContextMenuRequested.connect(self.recipe_menu)
        self.tabs.setTabText(1, "步骤使用说明")

    def accept_tree_order(self):
        old = {n["step_id"]: n for _, n in flatten(self._body["program_steps"])}
        seen = set()

        def collect(parent):
            result = []
            for i in range(parent.childCount()):
                item = parent.child(i)
                identity = item.data(0, Qt.UserRole)
                if not identity:
                    continue
                sid = identity[0]
                if sid == "__common":
                    if item.childCount():
                        raise ValueError("公共参数不能包含步骤")
                    continue
                if sid not in old or sid in seen:
                    raise ValueError("拖动后的步骤编号不完整")
                seen.add(sid)
                node = deepcopy(old[sid])
                children = collect(item)
                if children and node["kind"] != "point_loop":
                    raise ValueError("只有点位循环可以包含子步骤")
                if node["kind"] == "point_loop":
                    node["children"] = children
                result.append(node)
            return result

        try:
            nodes = collect(self.step_tree.invisibleRootItem())
            if seen != set(old):
                raise ValueError("拖动不能丢失步骤")
            self._body["program_steps"] = nodes
            self.mark_dirty()
            self.refresh_step_tree()
            self.refresh_params()
        except ValueError as error:
            self.validation_hint.setText(str(error))
            self.refresh_step_tree()

    def recipe_menu(self, pos):
        item = self.recipe_tree.itemAt(pos)
        if item is None:
            return
        identity = item.data(0, Qt.UserRole)
        if not identity and item.childCount():
            identity = item.child(0).data(0, Qt.UserRole)
        if not identity:
            return
        self.delete_menu = QMenu(self)
        self.delete_action = self.delete_menu.addAction("删除配方（全部版本）")
        self.delete_action.triggered.connect(lambda: self.delete_recipe(identity[0]))
        self.delete_menu.popup(self.recipe_tree.viewport().mapToGlobal(pos))

    def delete_recipe(self, rid):
        if self.app.workflow and self.app.workflow.busy:
            self.validation_hint.setText("任务执行中不能删除配方")
            return
        self.library.delete_recipe(rid, by=self._operator())
        if self.current_id == rid:
            self.current_id = self.current_rev = None
            self._body = None
            self._dirty = False
            self._selected_step_id = None
        self.refresh()
        self.validation_hint.setText("配方已从列表删除，历史运行快照保留。")
        self.app.pages["sequence"].on_show()

    def save_template(self):
        if not self._body or not self._body.get("program_steps"):
            self.validation_hint.setText("请先编排至少一个步骤")
            return
        name, ok = QInputDialog.getText(
            self,
            "保存流程模板",
            "模板名称（保存当前步骤、参数和公共参数；不会放行配方）",
        )
        if not ok:
            return
        try:
            self.templates.save(name, self._collect_body())
            self.validation_hint.setText("模板已保存，下次启动仍可从“插入模板”选择。")
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "保存模板失败", str(error))

    def choose_template(self):
        if self._read_only():
            self.validation_hint.setText("先复制为草稿后插入模板")
            return
        try:
            custom = self.templates.load()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "模板读取失败", str(error))
            return
        names = ["空白流程", "多点物料测角", "单点上下 mark"] + [
            f"自定义 · {t['name']} · {t['id'][:4]}" for t in custom
        ]
        name, ok = QInputDialog.getItem(
            self,
            "插入模板",
            "空白配方会载入自定义模板的公共参数；已有流程仅插入步骤",
            names,
            0,
            False,
        )
        if not ok:
            return
        index = names.index(name)
        if index < 3:
            nodes = insert_template(name, self._body)
        else:
            entry = custom[index - 3]
            nodes = self.templates.instantiate(entry)
            if not self._body["program_steps"]:
                self._body["common_params"] = deepcopy(entry.get("common_params", {}))
                if entry.get("coordinate_system") is not None:
                    self._body["coordinate_system"] = entry["coordinate_system"]
            if entry.get("example"):
                self._body["example"] = True
        self._body["program_steps"].extend(nodes)
        self.mark_dirty()
        self.refresh_step_tree()
        self.refresh_params()
