"""表面测角 page: recipe, point path, batch AF scan, fit results."""

import json
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFileDialog, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QWidget,
)

from zen_fit import FIELD_CONVENTION, validate_recipe
from zen_recipe import load_points, load_recipe
from zen_records import write_json
from zen_workflow import Recipe
from .. import theme
from ..dialogs import ConfirmRunDialog, PathEditorDialog, RecipeDialog
from ..plot import PointsCanvas
from ..widgets import Card, CheckItem, Checklist, Pill, StepStrip, hbox, label, note, vbox
from .device import _page


class TiltPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.point_status = {}
        self.active_index = None
        lay, head = _page(self, "表面测角", "逐点 AF 取 Z → 平面拟合 → Rx / Ry / RMS / PV")
        self.steps = StepStrip(["配方", "点位", "起点校验", "运行", "结果"])
        head.addWidget(self.steps)

        # ---- KPI row ----
        kpi_row = QWidget()
        kl = hbox(kpi_row, spacing=11, margins=0)
        lay.addWidget(kpi_row)
        self.kpi = {}
        for key, title, unit in (("rx", "Rx", "µrad"), ("ry", "Ry", "µrad"),
                                 ("rms", "RMS", "µm"), ("pv", "PV", "µm")):
            card = QWidget()
            card.setObjectName("card")
            cl = vbox(card, spacing=2, margins=(14, 9, 14, 9))
            head = label(title, role="muted")
            head.setStyleSheet("font-size:11px; font-weight:600")
            cl.addWidget(head)
            row = QWidget()
            rl = hbox(row, spacing=4, margins=0)
            value = label("—", role="num" if key in ("rx", "ry") else "numDark")
            rl.addWidget(value)
            unit_l = label(unit, role="muted")
            unit_l.setContentsMargins(0, 8, 0, 0)
            rl.addWidget(unit_l)
            rl.addStretch(1)
            cl.addWidget(row)
            self.kpi[key] = value
            kl.addWidget(card, 1)

        # ---- run bar ----
        bar = QWidget()
        bar.setObjectName("bar")
        bl = hbox(bar, spacing=9, margins=(12, 8, 12, 8))
        self.run_pill = Pill("未运行")
        bl.addWidget(self.run_pill)
        self.run_label = label("—", role="muted")
        bl.addWidget(self.run_label)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(180)
        self.progress.setTextVisible(False)
        self.progress.hide()
        bl.addWidget(self.progress)
        bl.addStretch(1)
        bl.addWidget(label("配方", role="muted"))
        self.recipe_name = label("未加载")
        bl.addWidget(self.recipe_name)
        bl.addWidget(QPushButton("编辑…", clicked=self.edit_recipe))
        self.start_button = QPushButton("开始批量测角 ▶", clicked=self.run_batch)
        self.start_button.setObjectName("primary")
        bl.addWidget(self.start_button)
        self.stop_button = QPushButton("完成当前点后停止", clicked=self.app.stop)
        self.stop_button.setStyleSheet(f"color:{theme.BAD}; border-color:#e2a59d;")
        bl.addWidget(self.stop_button)
        lay.addWidget(bar)

        body = QWidget()
        bl2 = hbox(body, spacing=11, margins=0)
        lay.addWidget(body, 1)
        work = QWidget()
        wl = vbox(work, spacing=11, margins=0)
        bl2.addWidget(work, 2)

        plot_card = Card("XY 点位与残差", trailing=label("悬停看单点", role="muted"))
        self.canvas = PointsCanvas()
        self.canvas.setMinimumHeight(220)
        plot_card.add(self.canvas)
        legend = QWidget()
        ll = hbox(legend, spacing=14, margins=0)
        for color, text in ((theme.OK, "完成"), (theme.ACCENT, "当前"), ("#888888", "待测"), (theme.BAD, "失败")):
            chip = QLabel(f"<span style='color:{color}'>●</span> {text}")
            ll.addWidget(chip)
        ll.addStretch(1)
        plot_card.add(legend)
        wl.addWidget(plot_card, 1)

        table_card = Card("逐点明细", trailing=label("自动追加到 points.csv", role="muted"))
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ("#", "目标 XY µm", "AF 前 XY µm", "AF 后 XY µm", "AF Z µm", "状态", "失败阶段"))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setMinimumHeight(200)
        table_card.add(self.table)
        wl.addWidget(table_card, 1)

        side = QWidget()
        sl = vbox(side, spacing=11, margins=0)
        side.setFixedWidth(330)
        bl2.addWidget(side)

        recipe_card = Card("配方", trailing=QPushButton("加载…", clicked=self.choose_recipe))
        self.recipe_info = label("未加载现场配方；也可在离线演示中自动生成演示配方。", role="muted", wrap=True)
        recipe_card.add(self.recipe_info)
        buttons = QWidget()
        btl = QGridLayout(buttons)
        btl.setContentsMargins(0, 0, 0, 0)
        for index, (text, fn) in enumerate((('新建 / 编辑', self.edit_recipe), ('保存配方', self.save_recipe),
                         ('编辑点位路径', self.edit_path), ('CSV 导入', self.choose_points))):
            btl.addWidget(QPushButton(text, clicked=fn), index // 2, index % 2)
        recipe_card.add(buttons)
        sl.addWidget(recipe_card)

        points_card = Card("点位管理")
        row = QWidget()
        rl = vbox(row, spacing=6, margins=0)
        self.teach_button = QPushButton("示教当前 XY", clicked=self.teach)
        self.delete_button = QPushButton("删除选中点", clicked=self.delete_point)
        self.af_button = QPushButton("当前点单点 AF 校验", clicked=self.run_single_af)
        for b in (self.teach_button, self.delete_button, self.af_button):
            rl.addWidget(b)
        points_card.add(row)
        self.points_list = QListWidget()
        self.points_list.setFont(QFont("Consolas", 9))
        self.points_list.setMaximumHeight(120)
        points_card.add(self.points_list)
        sl.addWidget(points_card)

        checks_card = Card("运行前检查")
        self.checklist = Checklist()
        self.check_items = {
            "connected": CheckItem("设备已连接并就绪", "自动"),
            "recipe": CheckItem("配方有效", "自动"),
            "start": CheckItem("起点 XY 已单点 AF 校验", "自动"),
            "recovery": CheckItem("无待恢复状态", "自动"),
        }
        self.checklist.set_items(list(self.check_items.values()))
        checks_card.add(self.checklist)
        checks_card.add(note("失败策略：任一点失败即停止后续运动，保留已测点，不给出拟合结果。", kind="info"))
        sl.addWidget(checks_card)

        self.export_button = QPushButton("导出结果 JSON…", clicked=self.export)
        sl.addWidget(self.export_button)
        sl.addStretch(1)

        self.no_xy_note = note('无 XY 模式：角度扫描不可用，请切换到「对位采图」。')
        wl.insertWidget(0, self.no_xy_note)
        self.refresh_enabled()

    # ---- recipe & points ----

    def apply_recipe(self, recipe):
        self.app.field_recipe = recipe
        self.app.field_verified = False
        workflow = self.app.workflow
        if workflow and not workflow.simulated:
            workflow.invalidate()
            self.table.setRowCount(0)
            self.point_status.clear()
        self.recipe_name.setText(recipe.name)
        self.recipe_info.setText(
            f"{recipe.experiment} · XY 容差 {recipe.tolerance_m*1e6:g} µm · "
            f"AF 超时 {recipe.af_timeout:g} s · 到位 {recipe.arrival_timeout:g} s · {len(recipe.targets)} 点")
        self.points_list.clear()
        for i, (x, y) in enumerate(recipe.targets, 1):
            self.points_list.addItem(QListWidgetItem(f"{i}. {x*1e6:g}, {y*1e6:g}"))
        self._redraw()
        self.refresh_enabled()

    def choose_recipe(self):
        if self.app.workflow and self.app.workflow.busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "加载现场配方", "", "配方 JSON (*.json)")
        if not path:
            return
        try:
            recipe = load_recipe(Path(path))
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            self.app.log("error", "配方读取失败，请检查版本、字段和单位。")
            return
        self.apply_recipe(recipe)

    def save_recipe(self):
        recipe = self.app.field_recipe
        if recipe is None or (self.app.workflow and self.app.workflow.busy):
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存现场配方", "", "现场配方 (*.json)")
        if not path:
            return
        try:
            destination = Path(path)
            if destination.resolve().is_relative_to(self.app.data_root.resolve()):
                self.app.log("error", "请选择运行记录目录之外的位置。")
                return
            write_json(destination, {
                "schema_version": 1, "coordinate_unit": "m",
                "recipe_name": recipe.name, "experiment_name": recipe.experiment,
                "point_order": recipe.targets, "xy_tolerance_m": recipe.tolerance_m,
                "af_timeout_s": recipe.af_timeout, "arrival_timeout_s": recipe.arrival_timeout,
                "sign_convention": recipe.sign_convention})
        except (OSError, ValueError):
            self.app.log("error", "保存配方失败，请检查目标目录及权限。")
        else:
            self.app.log("ok", "现场配方已保存，下次可直接加载。")

    def edit_recipe(self):
        if self.app.workflow and self.app.workflow.busy:
            return
        old = self.app.field_recipe
        dialog = RecipeDialog(self, old, FIELD_CONVENTION)
        if dialog.exec() and dialog.recipe:
            name, experiment, tolerance, af, arrival = dialog.recipe
            self.apply_recipe(Recipe(name, experiment, old.targets if old else (),
                                     tolerance, af, arrival, FIELD_CONVENTION))

    def edit_path(self):
        if not self.app.field_recipe or (self.app.workflow and self.app.workflow.busy):
            return
        dialog = PathEditorDialog(self, self.app.field_recipe.targets)
        if dialog.exec():
            self.apply_recipe(replace(self.app.field_recipe, targets=dialog.targets))
            self.app.log("info", f"路径已更新，共 {len(dialog.targets)} 点；请保存现场配方，并重新验证当前点 AF。")

    def choose_points(self):
        if not self.app.field_recipe or (self.app.workflow and self.app.workflow.busy):
            return
        path, _ = QFileDialog.getOpenFileName(self, "从 CSV 导入点位", "", "CSV (*.csv)")
        if not path:
            return
        try:
            targets = load_points(Path(path))
        except (OSError, ValueError, KeyError):
            self.app.log("error", "点位 CSV 读取失败，请检查列名（x_m,y_m 或 x_um,y_um）与数值。")
            return
        self.apply_recipe(replace(self.app.field_recipe, targets=targets))
        self.app.log("ok", f"已导入 {len(targets)} 个点，请保存配方并重新验证。")

    def teach(self):
        try:
            self.app.workflow.teach_xy()
        except (AttributeError, ValueError, RuntimeError) as exc:
            self.app.log("error", str(exc))

    def apply_taught(self, xy):
        recipe = self.app.field_recipe
        if recipe is None:
            return
        self.apply_recipe(replace(recipe, targets=recipe.targets + (tuple(xy),)))
        self.app.log("ok", f"已把当前 XY 示教为第 {len(self.app.field_recipe.targets)} 点")

    def delete_point(self):
        recipe = self.app.field_recipe
        if recipe is None or (self.app.workflow and self.app.workflow.busy):
            return
        index = self.points_list.currentRow()
        if not 0 <= index < len(recipe.targets):
            self.app.log("info", "请先在点位列表中选中要删除的点。")
            return
        targets = tuple(p for i, p in enumerate(recipe.targets) if i != index)
        self.apply_recipe(replace(recipe, targets=targets))

    # ---- run ----

    def field_ready(self):
        workflow = self.app.workflow
        return bool(workflow and not workflow.busy and workflow.state in ("就绪", "完成", "已中止")
                    and workflow.xy_enabled and not self.app.locked())

    def run_single_af(self):
        recipe = self.app.field_recipe
        if not recipe or not self.field_ready():
            return
        try:
            self.app.workflow.start(recipe, path_confirmed=True, single_point=True)
        except (RuntimeError, ValueError) as exc:
            self.app.log("error", str(exc))

    def run_batch(self):
        recipe = self.app.field_recipe
        workflow = self.app.workflow
        if not recipe or not self.field_ready() or not self.app.field_verified:
            return
        try:
            validate_recipe(recipe)
        except ValueError as exc:
            self.app.log("error", str(exc))
            return
        dialog = ConfirmRunDialog(self, recipe, self.app.verified_xy)
        if not dialog.exec():
            return
        self.table.setRowCount(0)
        self.point_status.clear()
        self.active_index = None
        self.progress.setMaximum(len(recipe.targets))
        self.progress.setValue(0)
        self.progress.show()
        self._redraw()
        try:
            workflow.start(recipe, path_confirmed=True, confirmed_start=self.app.verified_xy)
        except (RuntimeError, ValueError) as exc:
            self.app.log("error", str(exc))

    def export(self):
        workflow = self.app.workflow
        if not workflow or workflow.busy or workflow.result_path is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出结果", "", "JSON (*.json)")
        if not path:
            return
        try:
            run = workflow.result_path
            recipe = json.loads((Path(run) / "recipe.json").read_text(encoding="utf-8"))
            write_json(Path(path), {**workflow.result, "run_id": Path(run).name,
                                    "recipe": recipe, "simulated": workflow.simulated})
        except (OSError, ValueError, TypeError):
            self.app.log("error", "导出失败，请检查目标目录。")
        else:
            self.app.log("ok", "结果已导出。")

    # ---- events ----

    def on_workflow_event(self, kind, data, workflow):
        if kind == "state":
            state, point, detail = data
            self.active_index = point or None
            label_map = {"连接检查": "连接检查中…", "对位采图": "对位采图…"}
            self.run_label.setText(label_map.get(state, f"{state} · 点 {point or '—'} {detail}".rstrip()))
            self._redraw()
        elif kind == "row":
            row = data[0]
            self._append_row(row)
            self.point_status[row.index] = row.status
            self.progress.setValue(row.index)
            self._redraw()
        elif kind == "result":
            result, _ = data
            if result:
                for key, value in (("rx", result["rx_urad"]), ("ry", result["ry_urad"]),
                                   ("rms", result["rms_m"] * 1e6), ("pv", result["pv_m"] * 1e6)):
                    self.kpi[key].setText(f"{value:+.3f}" if key in ("rx", "ry") else f"{value:.3f}")
        elif kind == "taught":
            self.apply_taught(data[0])
        elif kind == "xy":
            self._redraw()

    def _append_row(self, row):
        def xy_text(xy):
            return "—" if xy is None else f"{xy[0]*1e6:.3f},{xy[1]*1e6:.3f}"
        state = {"ok": "完成", "failed": "失败", "stopped": "中止"}[row.status]
        index = self.table.rowCount()
        self.table.insertRow(index)
        values = (str(row.index), xy_text(row.target), xy_text(row.before_af),
                  xy_text(row.after_af), "—" if row.z is None else f"{row.z*1e6:.3f}",
                  state, row.failure_stage or "—")
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            if row.status == "failed":
                item.setForeground(QColor(theme.BAD))
            elif row.status == "ok":
                item.setForeground(QColor(theme.OK))
            self.table.setItem(index, col, item)
        self.table.scrollToBottom()

    def _redraw(self):
        recipe = self.app.field_recipe
        points = recipe.targets if recipe else ()
        self.canvas.set_data(points, status=self.point_status, active=self.active_index,
                             current_xy=self.app.current_xy)

    def clear_result(self):
        for value in self.kpi.values():
            value.setText("—")

    def refresh_enabled(self):
        app = self.app
        workflow = app.workflow
        busy = bool(workflow and workflow.busy)
        ready = self.field_ready()
        recipe = app.field_recipe
        self.no_xy_note.setVisible(app.xy_mode == 'no_xy')
        self.start_button.setEnabled(ready and app.field_verified and recipe is not None)
        self.stop_button.setEnabled(busy)
        self.teach_button.setEnabled(ready and recipe is not None)
        self.delete_button.setEnabled(ready and recipe is not None)
        self.af_button.setEnabled(ready and recipe is not None)
        self.export_button.setEnabled(bool(workflow and workflow.result is not None and not busy))
        self.run_pill.set_kind(
            "accent" if busy else "ok" if workflow and workflow.state in ("就绪", "完成") else "mute",
            "运行中" if busy else (workflow.state if workflow else "未运行"))
        # step strip
        completed = bool(workflow and workflow.result)
        states = ["done" if recipe else "current",
                  "done" if recipe and recipe.targets else "current",
                  "done" if app.field_verified or completed else "current",
                  "current" if busy else "todo",
                  "done" if workflow and workflow.result else "todo"]
        self.steps.set_states(states)
        # checks
        self.check_items["connected"].set_state(
            "ok" if workflow and workflow.state in ("就绪", "完成", "已中止") else "todo",
            "自动" if workflow else "未连接")
        if recipe is None:
            self.check_items["recipe"].set_state("todo", "未加载")
        else:
            try:
                validate_recipe(recipe)
            except (ValueError, TypeError) as exc:
                self.check_items["recipe"].set_state("warn", str(exc))
            else:
                self.check_items["recipe"].set_state("ok", f"{recipe.name} · {len(recipe.targets)} 点")
        self.check_items["start"].set_state("ok" if app.field_verified else "todo",
                                            "已完成单点 AF 校验" if app.field_verified else "需先做当前点单点 AF")
        self.check_items["recovery"].set_state("ok" if not app.locked() else "bad")
