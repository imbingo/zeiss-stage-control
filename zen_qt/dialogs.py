"""Modal dialogs: gateway settings, recipe editor, path editor, run/recovery
confirmation and the capture common-parameter sheet."""

from dataclasses import replace
from math import isfinite
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from zen_workflow import Recipe
from . import theme
from .widgets import Card, hbox, label, note, vbox


def _form_row(form, label_text, value="", password=False, browse=None):
    row = QWidget()
    lay = hbox(row, spacing=6, margins=0)
    entry = QLineEdit(value)
    if password:
        entry.setEchoMode(QLineEdit.Password)
    lay.addWidget(entry, 1)
    if browse:
        lay.addWidget(QPushButton("选择…", clicked=browse))
    form.addRow(label_text, row)
    return entry


class GatewayDialog(QDialog):
    """Same fields as the Tk gateway sheet; token stays session-only."""

    def __init__(self, app, parent=None):
        super().__init__(parent or app)
        self.app = app
        self.setWindowTitle("Gateway 连接设置")
        self.setMinimumWidth(560)
        lay = vbox(self, spacing=10, margins=16)
        lay.addWidget(note(
            "本机默认配置可留空。远程连接请填写地址、CA 和令牌。", kind="info"))
        form = QFormLayout()
        form.setSpacing(8)
        self.xy_mode = QComboBox()
        self.xy_mode.addItem('有电动 XY · 测角与采图', 'motorized')
        self.xy_mode.addItem('无电动 XY · 仅单点采图', 'no_xy')
        self.xy_mode.setCurrentIndex(max(0, self.xy_mode.findData(app.xy_mode)))
        form.addRow('设备模式', self.xy_mode)
        overrides = app.gateway_overrides
        self.ca_entry = QLineEdit(overrides.get("ZEN_GATEWAY_CA", ""))

        def browse():
            path, _ = QFileDialog.getOpenFileName(self, "CA 证书", "", "PEM (*.pem);;所有文件 (*)")
            if path:
                self.ca_entry.setText(path)
        self.address = _form_row(form, "地址与端口", overrides.get("ZEN_GATEWAY_ADDRESS", ""))
        form.addRow("CA 证书路径", hline_helper(self.ca_entry, browse))
        self.token = _form_row(form, "控制令牌", overrides.get("ZEN_GATEWAY_TOKEN", ""), password=True)
        self.allow_v1 = QCheckBox("允许 Focus v1 降级（仅旧版 ZEN；v1 单位 µm，会自动换算为 m）")
        self.allow_v1.setChecked(_env_flag(overrides, "ZEN_ALLOW_FOCUS_V1"))
        self.allow_unverified_af = QCheckBox("允许实验内置 AF 范围（AF 参数接口不可用时，按 ZEN 实验已存范围执行）")
        self.allow_unverified_af.setChecked(_env_flag(overrides, "ZEN_ALLOW_UNVERIFIED_AF"))
        self.allow_definite = QCheckBox("使用 Definite Focus 硬件对焦（软件 AF 服务缺失时，红外反射式表面检测）")
        self.allow_definite.setChecked(_env_flag(overrides, "ZEN_ALLOW_DEFINITE_FOCUS"))
        lay.addLayout(form)
        for box in (self.allow_v1, self.allow_unverified_af, self.allow_definite):
            lay.addWidget(box)
        buttons = QDialogButtonBox()
        apply_ = buttons.addButton("只读测试连接", QDialogButtonBox.AcceptRole)
        apply_.setObjectName("primary")
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        lay.addWidget(label("地址与证书会保存；令牌仅本次运行有效。", role="muted"))

    def _apply(self):
        if self.app.workflow and self.app.workflow.busy:
            return
        overrides = {}
        for env_key, entry in (("ZEN_GATEWAY_ADDRESS", self.address),
                               ("ZEN_GATEWAY_CA", self.ca_entry),
                               ("ZEN_GATEWAY_TOKEN", self.token)):
            value = entry.text().strip()
            if value:
                overrides[env_key] = value
        for env_key, box in (("ZEN_ALLOW_FOCUS_V1", self.allow_v1),
                             ("ZEN_ALLOW_UNVERIFIED_AF", self.allow_unverified_af),
                             ("ZEN_ALLOW_DEFINITE_FOCUS", self.allow_definite)):
            if box.isChecked():
                overrides[env_key] = "1"
        self.app.set_xy_mode(self.xy_mode.currentData())
        self.app.gateway_overrides = overrides
        self.app.preferences.update(connection=overrides, xy_mode=self.app.xy_mode)
        self.accept()
        self.app.connect_field()


def hline_helper(entry, browse):
    row = QWidget()
    lay = hbox(row, spacing=6, margins=0)
    lay.addWidget(entry, 1)
    lay.addWidget(QPushButton("选择…", clicked=browse))
    return row


def _env_flag(env, key):
    return env.get(key, "").strip().lower() in ("1", "true", "yes")


class RecipeDialog(QDialog):
    def __init__(self, parent, recipe, convention):
        super().__init__(parent)
        self.recipe = None
        self.setWindowTitle("现场测量配方")
        self.setMinimumWidth(460)
        lay = vbox(self, spacing=10, margins=16)
        form = QFormLayout()
        form.setSpacing(8)
        self.name = _form_row(form, "配方名称", recipe.name if recipe else "现场配方")
        self.experiment = _form_row(form, "ZEN 软件 AF 实验名", recipe.experiment if recipe else "")
        self.tolerance = _form_row(form, "XY 容差 (µm)", str(recipe.tolerance_m * 1e6) if recipe else "1")
        self.af = _form_row(form, "AF 超时 (s)", str(recipe.af_timeout if recipe else 3))
        self.arrival = _form_row(form, "到位超时 (s)", str(recipe.arrival_timeout if recipe else 2))
        lay.addLayout(form)
        lay.addWidget(label(convention, role="muted", wrap=True))
        buttons = QDialogButtonBox()
        apply_ = buttons.addButton("应用配方", QDialogButtonBox.AcceptRole)
        apply_.setObjectName("primary")
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _apply(self):
        try:
            values = (float(self.tolerance.text()) * 1e-6, float(self.af.text()),
                      float(self.arrival.text()))
            if not self.name.text().strip() or not self.experiment.text().strip() \
                    or not all(isfinite(v) and v > 0 for v in values):
                raise ValueError
        except ValueError:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "配方无效", "请填写名称和实验名，容差及超时须为有限正数。")
            return
        self.recipe = (self.name.text().strip(), self.experiment.text().strip(), *values)
        self.accept()


class PathEditorDialog(QDialog):
    """Editable point list; mirrors the Tk path editor semantics."""

    def __init__(self, parent, targets):
        super().__init__(parent)
        self.targets = tuple(targets)
        self.setWindowTitle("编辑点位路径")
        self.resize(560, 420)
        lay = vbox(self, spacing=8, margins=14)
        lay.addWidget(label("按顺序逐点执行；单位为 µm。修改后须重新做单点 AF 校验。", role="muted"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("#", "X (µm)", "Y (µm)"))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        lay.addWidget(self.table, 1)
        buttons_row = QWidget()
        brow = hbox(buttons_row, spacing=8, margins=0)
        for text, fn in (("添加点", self.add), ("删除选中", self.delete),
                         ("上移", lambda: self.move(-1)), ("下移", lambda: self.move(1))):
            brow.addWidget(QPushButton(text, clicked=fn))
        brow.addStretch(1)
        lay.addWidget(buttons_row)
        buttons = QDialogButtonBox()
        apply_ = buttons.addButton("应用路径", QDialogButtonBox.AcceptRole)
        apply_.setObjectName("primary")
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._reload()

    def _reload(self, select=None):
        self.table.setRowCount(len(self.targets))
        for row, (x, y) in enumerate(self.targets):
            for col, value in enumerate((str(row + 1), f"{x * 1e6:g}", f"{y * 1e6:g}")):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, col, item)
        if select is not None and self.targets:
            self.table.selectRow(max(0, min(select, len(self.targets) - 1)))

    def _values(self):
        points = []
        for row in range(self.table.rowCount()):
            x = float(self.table.item(row, 1).text())
            y = float(self.table.item(row, 2).text())
            if not all(map(isfinite, (x, y))):
                raise ValueError
            points.append((x * 1e-6, y * 1e-6))
        return points

    def _selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        return sorted(rows)

    def add(self):
        targets = list(self._values()) if self.table.rowCount() else list(self.targets)
        targets.append((0.0, 0.0))
        self.targets = tuple(targets)
        self._reload(len(self.targets) - 1)

    def delete(self):
        rows = self._selected()
        if not rows:
            return
        values = self._values()
        self.targets = tuple(p for i, p in enumerate(values) if i not in rows)
        self._reload(rows[0])

    def move(self, delta):
        rows = self._selected()
        if len(rows) != 1:
            return
        row = rows[0]
        values = list(self._values())
        new = row + delta
        if not 0 <= new < len(values):
            return
        values[row], values[new] = values[new], values[row]
        self.targets = tuple(values)
        self._reload(new)

    def _apply(self):
        try:
            self.targets = tuple(self._values())
        except (ValueError, AttributeError):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "点位无效", "X/Y 须为有限数字（µm）。")
            return
        self.accept()


class ConfirmRunDialog(QDialog):
    """Shows the actual motion plan before a batch scan (mirrors Tk dialog)."""

    def __init__(self, parent, recipe, start_xy):
        super().__init__(parent)
        self.setWindowTitle("确认批量运动")
        self.setMinimumWidth(460)
        lay = vbox(self, spacing=10, margins=16)
        lay.addWidget(note("核对实际起点到首点、各段路径与物镜间隙，确认后执行 XY 和 AF。"))
        text = QPlainTextEdit()
        text.setReadOnly(True)
        plan = [f"起点（单点 AF 后）X {start_xy[0] * 1e6:.3f} µm   Y {start_xy[1] * 1e6:.3f} µm"]
        plan += [f"{i}. X {x * 1e6:.3f} µm   Y {y * 1e6:.3f} µm"
                 for i, (x, y) in enumerate(recipe.targets, 1)]
        text.setPlainText("\n".join(plan))
        text.setMinimumHeight(min(16, len(plan) + 1) * 21)
        lay.addWidget(text, 1)
        buttons = QDialogButtonBox()
        ok = buttons.addButton("确认并执行", QDialogButtonBox.AcceptRole)
        ok.setObjectName("moving")
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


class RecoveryDialog(QDialog):
    """Operator attestation that every physical subsystem has actually stopped."""

    CHECKS = (
        ("xy_idle", "已按现场规程确认 XY：有电动 XY 时动作已结束；无电动 XY 时样品已固定、手动水平调整已结束", "xy_box"),
        ("af_z_idle", "已在 ZEN 或设备端独立确认软件 AF、Z 轴和拍摄均已结束", "z_box"),
        ("experiment_idle", "已确认 ZEN 实验与采集已停止，没有后台任务在运行", "experiment_box"),
        ("optics_unchanged", "已确认物镜、倍率与光路未被改变，转盘不在动作中", "optics_box"),
    )

    def __init__(self, app, parent=None):
        super().__init__(parent or app)
        self.app = app
        self.setWindowTitle("设备状态恢复确认")
        self.setMinimumWidth(560)
        lay = vbox(self, spacing=10, margins=16)
        lay.addWidget(note(
            "软件无法确认物理动作是否已结束。请先在 ZEN / 设备端独立核实，再逐项确认解锁。"))
        self.checks = []
        for key, text, attr in self.CHECKS:
            box = QCheckBox()
            row = QWidget()
            row_lay = hbox(row, spacing=8, margins=0)
            caption = label(text, wrap=True)
            box.setText("已确认")
            box.setAccessibleName(text)
            row_lay.addWidget(box)
            row_lay.addWidget(caption, 1)
            lay.addWidget(row)
            setattr(self, attr, box)
            self.checks.append((key, text, box))
        current = ""
        source = getattr(app, "operator_edit", None)
        if source is not None and hasattr(source, "text"):
            current = source.text()
        lay.addWidget(label("确认人（写入逐条恢复记录）", role="muted"))
        self.operator_edit = QLineEdit(current)
        self.operator_edit.setPlaceholderText("操作员姓名 / 工号")
        lay.addWidget(self.operator_edit)
        runs = app.pending_runs()
        self.list_label = label("待确认运行记录："
                                + ("、".join(p.name for p in runs) if runs else "（无）"),
                                role="muted", wrap=True)
        self.list_label.setAccessibleName("待确认运行记录列表")
        lay.addWidget(self.list_label)
        lay.addWidget(label("每条运行将分别写入一条带确认人、时间与设备回读的记录。",
                            role="muted", wrap=True))
        buttons = QDialogButtonBox()
        ok = buttons.addButton("记录恢复并解锁", QDialogButtonBox.AcceptRole)
        ok.setObjectName("danger")
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _confirm(self):
        missing = [text for _, text, box in self.checks if not box.isChecked()]
        if missing:
            self.list_label.setText(f"还有 {len(missing)} 项未确认：请逐项在设备端核实后勾选。")
            return
        attestation = {
            "operator": self.operator_edit.text().strip(),
            "reason": "设备状态恢复解锁",
            "items": [{"key": key, "label": text, "confirmed": True,
                       "method": "operator_external_check", "value": ""}
                      for key, text, box in self.checks],
        }
        if self.app.confirm_recovery(attestation):
            self.accept()
