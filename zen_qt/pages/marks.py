"""对位采图 page: teach Z, preflight, Z envelope, guarded capture, previews."""

import json
from copy import deepcopy
from math import isfinite
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QBoxLayout, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QWidget,
)

from zen_marks import MarkLayer, MarkRecipe
from .. import theme
from ..widgets import (Card, CheckItem, Checklist, Pill, StepStrip, ZEnvelope,
                       hbox, label, note, vbox)
from .device import _page

UM = 1e-6


class CommonParamsDialog(QDialog):
    """Common tolerances / timeouts shared by both layers."""

    FIELDS = (
        ("xy_tolerance_um", "XY 容差 (µm)", 1.0, 0.01, 1000.0),
        ("z_tolerance_um", "Z 容差 (µm)", 0.5, 0.01, 1000.0),
        ("af_timeout_s", "AF 超时 (s)", 10.0, 0.5, 600.0),
        ("arrival_timeout_s", "到位超时 (s)", 5.0, 0.5, 600.0),
        ("capture_timeout_s", "拍摄超时 (s)", 60.0, 1.0, 3600.0),
        ("settle_s", "稳定等待 (s)", 0.2, 0.001, 60.0),
    )

    def __init__(self, parent, values):
        super().__init__(parent)
        self.setWindowTitle("公共参数")
        lay = QFormLayout(self)
        self.spins = {}
        for key, text, default, lo, hi in self.FIELDS:
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setDecimals(3)
            spin.setValue(float(values.get(key, default)))
            lay.addRow(text, spin)
            self.spins[key] = spin
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addRow(buttons)

    def values(self):
        return {key: spin.value() for key, spin in self.spins.items()}


class MarksPage(QWidget):
    LAYER_TITLES = {"upper": "上 Mark", "lower": "下 Mark"}
    LAYER_DEFAULT_EXPERIMENT = {"upper": "Demo upper", "lower": "Demo lower"}

    def __init__(self, app):
        super().__init__()
        self.app = app
        self._last_inspect = None
        self.layers = {}
        self._restoring = True
        self._configuration_simulated = False
        self._field_preferences = None
        self._last_result_output = None
        self._preview_images = {}
        self._unparsed_entries = {}
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self.persist)
        self.common = {"xy_tolerance_um": 1.0, "z_tolerance_um": 0.5, "af_timeout_s": 10.0,
                       "arrival_timeout_s": 5.0, "capture_timeout_s": 60.0, "settle_s": 0.2}
        lay, head = _page(self, "对位采图",
                          "在当前 XY 位置 → 分别到上、下 mark 示教 Z 拍一张 → 保存 TIFF + JSON 元数据")
        self.ready_button=QPushButton('检查设备就绪',clicked=self.check_device_ready)
        head.addWidget(self.ready_button)
        self.steps = StepStrip(["设备就绪", "示教 Z", "检查", "采集", "结果"])
        head.addWidget(self.steps)

        body = QWidget()
        bl = hbox(body, spacing=11, margins=0)
        lay.addWidget(body, 1)
        work = QWidget()
        wl = vbox(work, spacing=11, margins=0)
        bl.addWidget(work, 1)
        side = QWidget()
        self._side_lay = vbox(side, spacing=11, margins=0)
        side.setFixedWidth(330)
        work.setMinimumWidth(530)
        bl.addWidget(side)

        # ---- layer cards + Z envelope ----
        row = QWidget()
        rl = hbox(row, spacing=11, margins=0)
        self._layer_layout = rl
        wl.addWidget(row)
        for key in ("upper", "lower"):
            card = Card(self.LAYER_TITLES[key])
            card.setMinimumHeight(210)
            card._lay.insertWidget(1, self._layer_card(key, card))
            rl.addWidget(card, 1)

        env_card = Card("Z 包络与运动预览",
                        trailing=label("示教位 ± AF 半范围；当前 Z 实时更新", role="muted"))
        env_card.setMinimumHeight(160)
        env_row = QWidget()
        el = hbox(env_row, spacing=11, margins=0)
        self.envelope = ZEnvelope()
        el.addWidget(self.envelope)
        env_info = QWidget()
        vil = vbox(env_info, spacing=5, margins=0)
        self.env_text = label("—", wrap=True, mono=True)
        self.env_text.setStyleSheet("font-size:11.5px; line-height:1.7")
        vil.addWidget(self.env_text)
        vil.addStretch(1)
        vil.addWidget(note('建议使用已在 ZEN 验证的单平面实验。检测到 Z-stack 时仅在临时实验上尝试收敛；'
                           '软件位置检查不等于防撞保护。', kind='info'))
        el.addWidget(env_info, 1)
        env_card.add(env_row)
        wl.addWidget(env_card)

        # ---- captured images ----
        images_row = QWidget()
        il = hbox(images_row, spacing=11, margins=0)
        self._image_layout = il
        wl.addWidget(images_row, 1)
        self.image_cards = {}
        self.saved_pills = {}
        self.image_labels = {}
        self.path_labels = {}
        for key in ("upper", "lower"):
            card = Card(f"{self.LAYER_TITLES[key]}图像")
            card.setMinimumHeight(250)
            saved = Pill("未拍摄")
            self.saved_pills[key] = saved
            card._lay.itemAt(0).widget().layout().addWidget(saved)
            img = QLabel("—")
            img.setAlignment(Qt.AlignCenter)
            img.setMinimumHeight(140)
            img.setStyleSheet(f"background:{theme.INSET}; border-radius:8px; color:{theme.MUTED};")
            card.add(img)
            path_label = label("—", role="muted", wrap=True, mono=True)
            path_label.setStyleSheet("font-size:10px")
            card.add(path_label)
            self.image_cards[key] = card
            self.image_labels[key] = img
            self.path_labels[key] = path_label
            il.addWidget(card, 1)

        # ---- right side: capture options ----
        opt_card = Card("采集设置")
        form = QFormLayout()
        form.setSpacing(7)
        self.use_af = QCheckBox("拍摄前 AF 精修焦面")
        self.use_af.setChecked(True)
        form.addRow(self.use_af)
        self.capture_kind = QComboBox()
        self.capture_kind.addItem("相机快照 (camera_snap)", "camera_snap")
        self.capture_kind.addItem("共聚焦 (confocal)", "confocal")
        form.addRow("采集方式", self.capture_kind)
        self.output_edit = QLineEdit(str(Path(app.data_root).parent / "ZENMarkImages"))
        browse = QPushButton("浏览…", clicked=self._choose_output)
        out_row = QWidget()
        ol = hbox(out_row, spacing=6, margins=0)
        ol.addWidget(self.output_edit, 1)
        ol.addWidget(browse)
        form.addRow("图像输出", out_row)
        self.common_button = QPushButton("公共参数（容差 / 超时）…", clicked=self._edit_common)
        form.addRow(self.common_button)
        opt_card.add_layout(form)
        self._side_lay.addWidget(opt_card)

        checks_card = Card("采集前检查", trailing=label("全部通过才可拍摄", role="muted"))
        self.checklist = Checklist()
        self.check_items = {
            "connected": CheckItem("设备已连接并就绪", "自动"),
            "recipe": CheckItem("所选层均已示教 Z 且范围有效", "自动"),
            "experiments": CheckItem("拍摄实验已检查（建议）", "只读，不加载"),
            "zpath": CheckItem("已确认两层 Z 路径、AF 范围与物镜间隙", "", manual=True),
            "loading": CheckItem("已确认实验加载/拍摄不换物镜、不恢复位置", "", manual=True),
            "recovery": CheckItem("无待恢复状态", "自动"),
        }
        self.checklist.set_items(list(self.check_items.values()))
        checks_card.add(self.checklist)
        self._side_lay.addWidget(checks_card)

        run_card = Card("执行")
        self.inspect_button = QPushButton("只读检查拍摄实验", clicked=self.inspect)
        run_card.add(self.inspect_button)
        self.capture_buttons = {}
        for key, text in (("upper", "采集上 Mark"), ("lower", "采集下 Mark"),
                          ("both", "采集上 + 下 Mark")):
            button = QPushButton(text, clicked=lambda _=False, k=key: self._capture_clicked(k))
            if key == "both":
                button.setObjectName("primary")
            self.capture_buttons[key] = button
            run_card.add(button)
        run_card.add(note("拍摄将移动 Z 轴。AF 结果须在示教 ±半范围内；软件检查不能代替硬件限位。",
                          kind="warn"))
        self._side_lay.addWidget(run_card)

        diag_card = Card("诊断")
        self.diag_summary = label("无报告。实验检查与拍摄失败均在此汇总。", role="muted", wrap=True)
        diag_card.add(self.diag_summary)
        row2 = QWidget()
        rl2 = hbox(row2, spacing=6, margins=0)
        rl2.addWidget(QPushButton("查看报告", clicked=self._show_diagnostic))
        rl2.addWidget(QPushButton("导出诊断包…", clicked=self.app.export_diagnostic_bundle))
        rl2.addStretch(1)
        diag_card.add(row2)
        self._side_lay.addWidget(diag_card)
        self._side_lay.addStretch(1)

        self.restore()
        self._restoring = False
        self._field_preferences = None if self._configuration_simulated else self._serialize()
        self.use_af.toggled.connect(self._parameters_changed)
        self.capture_kind.currentIndexChanged.connect(self._parameters_changed)
        self.output_edit.textChanged.connect(self._parameters_changed)
        for key in ('zpath', 'loading'):
            self.check_items[key].check.toggled.connect(self.refresh_enabled)
        self.refresh_enabled()

    # ---- layer card ----

    def _layer_card(self, key, card):
        widget = QWidget()
        form = QFormLayout(widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(7)
        state = {}
        exp_row = QWidget()
        el = hbox(exp_row, spacing=6, margins=0)
        combo = QComboBox()
        combo.setEditable(True)
        el.addWidget(combo, 1)
        refresh = QPushButton("刷新", clicked=lambda: self.load_experiments())
        el.addWidget(refresh)
        form.addRow("拍摄实验", exp_row)
        z_row = QWidget()
        zl = hbox(z_row, spacing=6, margins=0)
        z_edit = QDoubleSpinBox()
        z_edit.setRange(-1e9, 1e9)
        z_edit.setDecimals(3)
        z_edit.setSuffix(" µm")
        zl.addWidget(z_edit, 1)
        teach = QPushButton("示教当前 Z", clicked=lambda _=False, k=key: self.teach_z(k))
        zl.addWidget(teach)
        form.addRow("示教 Z", z_row)
        half = QDoubleSpinBox()
        half.setRange(0.1, 10000.0)
        half.setDecimals(3)
        half.setSuffix(" µm")
        half.setValue(5.0)
        form.addRow("AF 半范围", half)
        taught = Pill("未示教")
        form.addRow("状态", taught)
        z_edit.valueChanged.connect(lambda *_, k=key: self._z_edited(k))
        half.valueChanged.connect(lambda *_ , k=key: self._range_edited(k))
        half.editingFinished.connect(lambda k=key: self._range_edited(k))
        combo.currentTextChanged.connect(self._parameters_changed)
        state.update({"combo": combo, "z": z_edit, "half": half, "taught": taught,
                      "teach_button": teach, "z_set": False})
        self.layers[key] = state
        return widget

    def _range_edited(self, key):
        if not self._restoring:
            self._unparsed_entries.pop((key, 'half_range_m'), None)
            self.layers[key]['range_valid'] = True
        self._parameters_changed()

    def _z_edited(self, key):
        if not self._restoring:
            self._unparsed_entries.pop((key, 'z_m'), None)
        state = self.layers[key]
        if not state["z_set"]:
            state["z_set"] = True
            state["taught"].set_kind("ok", "已输入")
        self._parameters_changed()

    def invalidate_confirmation(self):
        for key in ('zpath', 'loading'):
            self.check_items[key].check.setChecked(False)
        self._last_inspect = None
        self.check_items['experiments'].set_state('todo', '参数改变后需重新检查')

    def _parameters_changed(self, *_):
        if self._restoring:
            return
        self.invalidate_confirmation()
        self._last_result_output = None
        self._update_derived()
        self._save_timer.start(200)

    # ---- persistence ----

    @staticmethod
    def _number(value, *, positive=False):
        if isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if isfinite(value) and (not positive or value > 0) else None

    def restore(self, prefs=None):
        prefs = self.app.preferences.data.get('mark_capture', {}) if prefs is None else prefs
        was_restoring = self._restoring
        self._restoring = True
        try:
            self._unparsed_entries.clear()
            self._configuration_simulated = bool(prefs.get('simulated_configuration', False))
            factor = 1 / UM if prefs.get('entry_unit') == 'm' else 1
            for key in ('upper', 'lower'):
                saved = prefs.get(key) or {}
                state = self.layers[key]
                state['combo'].setCurrentText(saved.get('experiment', ''))
                z = self._number(saved.get('z_m'))
                if z is not None and not state['z'].minimum() <= z * factor <= state['z'].maximum():
                    z = None
                if z is None and saved.get('z_m'):
                    self._unparsed_entries[(key, 'z_m')] = saved['z_m']
                state['z'].setValue(0 if z is None else z * factor)
                state['z_set'] = z is not None
                state['taught'].set_kind('warn' if z is not None else 'mute',
                                        '历史输入 · 请现场核对' if z is not None else '未示教')
                half = self._number(saved.get('half_range_m'), positive=True)
                if half is not None and not state['half'].minimum() <= half * factor <= state['half'].maximum():
                    half = None
                state['range_valid'] = half is not None or not saved.get('half_range_m')
                if not state['range_valid']:
                    self._unparsed_entries[(key, 'half_range_m')] = saved['half_range_m']
                state['half'].setValue(5 if half is None else half * factor)
            common = prefs.get('common') or {}
            for key in self.common:
                stored_key = key.replace('_um', '_m')
                value = self._number(common.get(stored_key), positive=True)
                if value is not None:
                    self.common[key] = value * factor if key.endswith('_um') else value
            self.use_af.setChecked(prefs.get('use_autofocus', True))
            index = self.capture_kind.findData(prefs.get('capture_kind', 'camera_snap'))
            self.capture_kind.setCurrentIndex(max(0, index))
            self.output_edit.setText(prefs.get('output', prefs.get('output_dir',
                                     str(Path(self.app.data_root).parent / 'ZENMarkImages'))))
        finally:
            self._restoring = was_restoring
        self.invalidate_confirmation()
        self._update_derived()

    def _serialize(self):
        common = {k.replace('_um', '_m'): str(v)
                  for k, v in self.common.items()}
        prefs = {'entry_unit': 'um', 'use_autofocus': self.use_af.isChecked(),
                 'capture_kind': self.capture_kind.currentData(),
                 'output': self.output_edit.text(), 'common': common,
                 'simulated_configuration': self._configuration_simulated}
        for key in ('upper', 'lower'):
            state = self.layers[key]
            prefs[key] = {'experiment': state['combo'].currentText(),
                          'z_m': str(state['z'].value()) if state['z_set'] else '',
                          'half_range_m': str(state['half'].value())}
            for field in ('z_m', 'half_range_m'):
                if (key, field) in self._unparsed_entries:
                    prefs[key][field] = self._unparsed_entries[(key, field)]
        return prefs

    def persist(self):
        if self._restoring:
            return
        # An offline demonstration must not replace the user's field profile.
        prefs = self._field_preferences if self._configuration_simulated and self._field_preferences else self._serialize()
        self.app.preferences.update(mark_capture=prefs)

    def load_demo_defaults(self):
        self._restoring = True
        self._configuration_simulated = True
        self._unparsed_entries.clear()
        try:
            for key in ('upper', 'lower'):
                state = self.layers[key]
                state['combo'].setCurrentText(self.LAYER_DEFAULT_EXPERIMENT[key])
                state['z'].setValue(100.0 if key == 'upper' else 200.0)
                state['z_set'] = True
                state['range_valid'] = True
                state['taught'].set_kind('warn', '演示 Z · 不用于现场')
                state['half'].setValue(5.0)
        finally:
            self._restoring = False
        self.invalidate_confirmation()
        self._update_derived()

    def on_device_changed(self, workflow):
        self._save_timer.stop()
        self.invalidate_confirmation()
        self._last_result_output = None
        self._preview_images.clear()
        for key in ('upper', 'lower'):
            self.image_labels[key].clear()
            self.image_labels[key].setText('—')
            self.saved_pills[key].set_kind('mute', '未拍摄')
            self.path_labels[key].setText('—')
        if workflow and workflow.simulated:
            if not self._configuration_simulated:
                self._field_preferences = deepcopy(self._serialize())
            self.load_demo_defaults()
        elif (workflow is None or not workflow.simulated) and self._configuration_simulated:
            self.restore(self._field_preferences or {})
            self._configuration_simulated = False
            self.persist()
        self._update_derived()

    # ---- recipe ----

    def build_recipe(self, layers):
        keys = tuple(layers)
        def layer(key):
            if key not in keys:
                return None
            state = self.layers[key]
            if not state["z_set"]:
                return None
            if not state.get('range_valid', True):
                raise ValueError('历史 AF 半范围无效，请重新填写')
            return MarkLayer(state["combo"].currentText().strip(),
                             state["z"].value() * UM, state["half"].value() * UM)
        if self.app.workflow and not self.app.workflow.simulated and self._configuration_simulated:
            raise ValueError('演示配置不能用于现场，请重新填写或示教')
        recipe = MarkRecipe(layer("upper"), layer("lower"),
                            xy_tolerance_m=self.common["xy_tolerance_um"] * UM,
                            z_tolerance_m=self.common["z_tolerance_um"] * UM,
                            af_timeout_s=self.common["af_timeout_s"],
                            arrival_timeout_s=self.common["arrival_timeout_s"],
                            capture_timeout_s=self.common["capture_timeout_s"],
                            settle_s=self.common["settle_s"],
                            xy_mode=self.app.xy_mode,
                            capture_kind=self.capture_kind.currentData(),
                            use_autofocus=self.use_af.isChecked())
        recipe.validate(keys)
        return recipe

    def _recipe_error(self, keys):
        try:
            self.build_recipe(keys)
            return None
        except (ValueError, TypeError) as exc:
            return str(exc)

    # ---- actions ----

    def teach_z(self, key):
        workflow = self.app.workflow
        if workflow is None or workflow.busy:
            self.app.log("info", "请先连接设备。")
            return
        try:
            workflow.read_mark_z(key)   # gated read; emits ("mark_z", layer, z, xy)
        except (RuntimeError, ValueError) as exc:
            self.app.log("error", str(exc))

    def _apply_mark_z(self, key, z, xy):
        state = self.layers[key]
        state["z"].setValue(z / UM)
        state["z_set"] = True
        state["taught"].set_kind("ok", f"已示教 {z / UM:.2f} µm")
        self.app.log("ok", f"{self.LAYER_TITLES[key]} Z 已示教：{z / UM:.2f} µm")
        self.persist()
        self._update_derived()

    def load_experiments(self):
        """Refresh the experiment name dropdown from the device (read-only)."""
        workflow = self.app.workflow
        if workflow is None or workflow.device is None:
            return
        if workflow.busy:
            return
        try:
            workflow.read_only('mark_experiments',
                lambda device: device.call_hardware_operation('list_experiments', {}))
        except RuntimeError as error:
            self.app.log('warn', str(error))

    def on_rpc_result(self, tag, result):
        if tag != 'mark_experiments':
            return
        names = [x['name'] for x in result.get('experiments', []) if isinstance(x, dict) and x.get('name')]
        for key in ('upper', 'lower'):
            combo = self.layers[key]['combo']
            text = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            combo.setCurrentText(text)
            combo.blockSignals(False)
        self.app.log('ok', f'已读取 {len(names)} 个实验名（未加载实验）')

    def on_rpc_error(self, tag, error):
        if tag == 'mark_experiments':
            self.app.log('error', '实验名读取失败：' + str(error))

    def inspect(self):
        workflow = self.app.workflow
        names = [self.layers[k]["combo"].currentText().strip()
                 for k in ("upper", "lower")
                 if self.layers[k]["combo"].currentText().strip()]
        if workflow is None or workflow.device is None or workflow.busy or not names:
            self.app.log("info", "请先连接设备并填写至少一个拍摄实验名。")
            return
        try:
            workflow.inspect_mark_experiments(tuple(names),
                                            reported_zen_version="operator-qt")
        except (RuntimeError, ValueError) as exc:
            self.app.log("error", str(exc))
        else:
            self.app.log("info", "正在只读检查拍摄实验…")

    def _capture_clicked(self, key):
        layers = ("upper", "lower") if key == "both" else (key,)
        self.capture(layers)

    def capture(self, layers):
        app = self.app
        workflow = app.workflow
        if workflow is None or workflow.busy:
            return
        try:
            recipe = self.build_recipe(layers)
        except (ValueError, TypeError) as exc:
            app.log("error", str(exc))
            return
        confirmed = self.check_items["zpath"].check.isChecked()
        loading = self.check_items["loading"].check.isChecked()
        output = self.output_edit.text().strip()
        if not output:
            app.log("error", "请选择图像输出目录。")
            return
        try:
            workflow.start_mark_capture(recipe, Path(output), layers=tuple(layers),
                                        confirmed=confirmed,
                                        experiment_loading_confirmed=loading)
        except (RuntimeError, ValueError) as exc:
            app.log("error", str(exc))
            return
        self.persist()
        self.invalidate_confirmation()
        for key in layers:
            self.saved_pills[key].set_kind("accent", "采集中…")
        app.log("info", f"开始采集 {len(layers)} 层…")

    def _choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "图像输出目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def _edit_common(self):
        dialog = CommonParamsDialog(self, self.common)
        if dialog.exec():
            self.common.update(dialog.values())
            self._parameters_changed()
            self.persist()

    def _show_diagnostic(self):
        report = getattr(self.app.workflow, "mark_diagnostic_report", None) or self._last_inspect
        if not report:
            self.app.log("info", "暂无诊断报告。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("诊断报告")
        dialog.resize(760, 560)
        lay = QFormLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(json.dumps(report, ensure_ascii=False, indent=2))
        lay.addRow(view)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        lay.addRow(buttons)
        dialog.exec()

    # ---- derived state / events ----

    LAYER_COLORS = {"upper": theme.ACCENT, "lower": theme.WARN}

    def _update_derived(self):
        ranges = {}
        for key in ("upper", "lower"):
            state = self.layers[key]
            if state["z_set"]:
                z_um, half_um = state["z"].value(), state["half"].value()
                ranges[key] = (z_um, half_um)
        current = self.app._live_z
        current_um = None if current is None else current / UM
        bands = [(self.LAYER_TITLES[k], z, h, self.LAYER_COLORS[k])
                 for k, (z, h) in ranges.items()]
        self.envelope.set_data(bands, current_um)
        lines = []
        for key in ("upper", "lower"):
            if key in ranges:
                z_um, half_um = ranges[key]
                lines.append(f"{self.LAYER_TITLES[key]}：{z_um:.2f} µm ± {half_um:.2f} µm "
                             f"→ [{z_um - half_um:.2f}, {z_um + half_um:.2f}]")
                if current_um is not None:
                    lines.append(f"　距当前 Z {abs(z_um - current_um):.2f} µm")
        overlap = None
        if len(ranges) == 2:
            a, b = ranges["upper"], ranges["lower"]
            overlap = not (a[0] + a[1] < b[0] - b[1] or b[0] + b[1] < a[0] - a[1])
        if overlap:
            lines.append("⚠ 上下层 AF 范围重叠，配方校验将拒绝拍摄")
        self.env_text.setText("\n".join(lines) or "尚未示教 Z")

    def on_connect(self):
        self.invalidate_confirmation()
        self._update_derived()
        self.refresh_enabled()

    def check_device_ready(self):
        w=self.app.workflow
        if w and not w.busy:
            w.verify_ready()

    def on_show(self):
        w=self.app.workflow
        if w and not w.busy and w.state=='通信就绪（硬件单步）':self.check_device_ready()

    def on_workflow_event(self, kind, data, workflow):
        if kind == "state":
            state, _point, detail = data
            if state == "对位采图":
                self.steps.set_states(["done", "done", "done", "current", "todo"])
            elif state == "就绪" and getattr(workflow, "mark_output_path", None):
                self._show_results(workflow)
            elif state == "设备状态待确认":
                self.steps.set_states(["done", "done", "done", "bad", "todo"])
        elif kind == "mark_z":
            layer, z, xy = data
            self._apply_mark_z(layer, z, xy)
        elif kind == "mark_diagnostic":
            self._last_inspect = data[0]
            self._summarize_diagnostic(data[0])
        elif kind == "mark_error":
            self.app.log("error", f"采图失败：{data[0]}")
            for key in ("upper", "lower"):
                if self.saved_pills[key].text() == "采集中…":
                    self.saved_pills[key].set_kind("bad", "失败")
        elif kind == "xy":
            self._update_derived()

    def _summarize_diagnostic(self, report):
        requested = report.get('requested_experiments', report.get('experiments', []))
        lines = []
        for exp in requested:
            if isinstance(exp, dict):
                exists = exp.get('exists')
                state = '名称存在' if exists is True else '名称不存在' if exists is False else '名称未确认'
                lines.append(f"{exp.get('name', '?')}：{state}")
        errors = list(report.get('errors') or [])
        if report.get('error'):
            errors.append(report['error'])
        for error in errors:
            lines.append('错误：' + str(error.get('message', error) if isinstance(error, dict) else error))
        success = (report.get('kind') == 'read_only_experiment_check' and bool(requested)
                   and not errors and all(e.get('exists') is True for e in requested))
        lines.append('此检查不加载实验；曝光、维度、物镜及位置恢复设置需在 ZEN 核对。')
        self.diag_summary.setText('\n'.join(lines))
        self.check_items['experiments'].set_state('ok' if success else 'warn',
            '名称已确认，硬件设置未核验' if success else '未通过/未完成，请查看报告')

    def _show_results(self, workflow):
        output = getattr(workflow, "mark_output_path", None)
        if output is None:
            return
        output = Path(output)
        self._last_result_output = output
        self.steps.set_states(["done", "done", "done", "done", "done"])
        for key in ("upper", "lower"):
            tiff = output / f"{key}.tif"
            if not tiff.exists():
                continue
            self.saved_pills[key].set_kind("ok", "已保存")
            image = self._preview(tiff)
            pixmap = QPixmap.fromImage(image)
            img_label = self.image_labels[key]
            if pixmap.isNull():
                img_label.setText("已保存（预览不可用）")
            else:
                self._preview_images[key] = pixmap
                img_label.setPixmap(pixmap.scaled(img_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.path_labels[key].setText(str(tiff))

    @staticmethod
    def _preview(path):
        image = QImage(str(path))
        if not image.isNull():
            return image
        # Display-only conversion: never write back to the original TIFF.
        import numpy as np
        import tifffile
        try:
            data = tifffile.imread(path)
            if data.ndim not in (2, 3) or (data.ndim == 3 and data.shape[2] != 3):
                return image
            if data.dtype == np.uint16:
                data = (data // 257).astype(np.uint8)
            if data.dtype != np.uint8:
                return image
            data = np.ascontiguousarray(data)
            fmt = QImage.Format_Grayscale8 if data.ndim == 2 else QImage.Format_RGB888
            return QImage(data.data, data.shape[1], data.shape[0], data.strides[0], fmt).copy()
        except (OSError, ValueError, TypeError):
            return image

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_layer_layout'):
            narrow = self.width() < 1250
            self._layer_layout.setDirection(QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight)
            self._image_layout.setDirection(QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight)
        for key, pixmap in self._preview_images.items():
            self.image_labels[key].setPixmap(pixmap.scaled(self.image_labels[key].size(),
                Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def refresh_enabled(self):
        app = self.app
        workflow = app.workflow
        busy = bool(workflow and workflow.busy)
        ready = bool(workflow and workflow.device is not None
                     and workflow.state in ("就绪", "完成", "已中止")
                     and not app.locked() and not busy)
        manual = all(self.check_items[k].check.isChecked() for k in ('zpath', 'loading'))
        both_error = self._recipe_error(("upper", "lower"))
        for key, button in self.capture_buttons.items():
            keys = ("upper", "lower") if key == "both" else (key,)
            error = self._recipe_error(keys)
            button.setEnabled(ready and manual and error is None)
            button.setToolTip(error or "")
        self.inspect_button.setEnabled(bool(workflow and workflow.device is not None and not busy))
        self.ready_button.setEnabled(bool(workflow and not busy and not app.locked()))
        manual = (self.check_items["zpath"].check.isChecked()
                  and self.check_items["loading"].check.isChecked())
        # checks
        connected = bool(workflow and workflow.device is not None)
        self.check_items["connected"].set_state(
            "ok" if workflow and workflow.state in ("就绪", "完成", "已中止") else "todo",
            ('正在校验' if workflow and workflow.state=='就绪校验' else workflow.state+' · 可点击检查设备就绪' if connected else '未连接'))
        if both_error is None:
            self.check_items["recipe"].set_state("ok", "两层配方有效")
        else:
            missing_any = all(self._recipe_error((k,)) for k in ("upper", "lower"))
            self.check_items["recipe"].set_state(
                "warn" if not missing_any else "todo",
                both_error if not missing_any else "尚未示教任何一层")
        self.check_items["recovery"].set_state("bad" if app.locked() else "ok")
        if not app.locked() and connected and not manual:
            for key in ("upper", "lower", "both"):
                self.capture_buttons[key].setToolTip(
                    self.capture_buttons[key].toolTip() or "请先勾选两项人工确认")
        # step strip (idle view)
        for state in self.layers.values():
            for name in ('combo', 'z', 'half', 'teach_button'):
                state[name].setEnabled(not busy)
        for control in (self.use_af, self.capture_kind, self.output_edit, self.common_button):
            control.setEnabled(not busy)
        if not busy and self._last_result_output is None:
            taught = sum(1 for k in ("upper", "lower") if self.layers[k]["z_set"])
            self.steps.set_states([
                "done" if connected else "current",
                "done" if taught else "current",
                "done" if manual else "current",
                "todo", "todo"])
