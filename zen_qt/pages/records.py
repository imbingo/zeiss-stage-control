"""记录与分析 page: run archive browser + index.csv export + trend sparkline."""

import csv
import json
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage,QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QWidget, QTabWidget, QPlainTextEdit,
)

from .. import theme
from ..widgets import Card, hbox, label, vbox
from .device import _page

KIND_NAMES = {"tilt": "测角", "mark": "采图", "hardware": "硬件单步", "sequence": "自动任务", "unknown": "记录"}
STATE_PILLS = {"complete": ("ok", "有效"), "stopped": ("warn", "中止"),
               "failed": ("bad", "失败"), "running": ("accent", "运行中")}
STATE_NAMES = {"complete": "已完成", "stopped": "已中止", "failed": "失败",
               "running": "未正常结束", "unknown": "状态未知"}
VERDICT_COLORS = {
    "ok": (theme.OK_SOFT, theme.OK),
    "warn": (theme.WARN_SOFT, theme.WARN),
    "bad": (theme.BAD_SOFT, theme.BAD),
}


def run_verdict(run):
    """(kind, headline) an operator can act on, without reading raw fields."""
    state = run["state"]
    disposition = run.get("sequence", {}).get("disposition")
    if state == "failed" or disposition == "EQUIPMENT_FAULT":
        stage = f" · 阶段：{run['stage']}" if run["stage"] else ""
        return "bad", ("设备异常" if disposition == "EQUIPMENT_FAULT" else "失败") + stage
    if disposition == "MEASUREMENT_REJECTED":
        return "bad", "角度不合格"
    if run["recovery"]:
        return "warn", STATE_NAMES.get(state, state) + " · 待恢复确认"
    if state == "stopped":
        return "warn", "已中止 · 结果不完整"
    if state != "complete":
        return "warn", STATE_NAMES.get(state, state)
    if run["kind"] == "sequence":
        return "ok", "采集完成 · 待 SOMA 对位分析"
    if run["kind"] == "mark":
        return "ok", "采图完成 · 待对位分析"
    if run["kind"] == "tilt":
        return ("ok", "测角结果有效") if run["valid"] else ("warn", "已完成 · 结果未判为有效")
    return "ok", "已完成"


def _load_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def scan_runs(data_root):
    """Collect every run directory under demo/ and field/."""
    runs = []
    for simulated, folder in ((True, "demo"), (False, "field")):
        root = Path(data_root) / folder
        if not root.is_dir():
            continue
        for run in sorted(root.iterdir(), reverse=True):
            if not run.is_dir():
                continue
            status = _load_json(run / "status.json")
            recipe = _load_json(run / "recipe.json")
            result = _load_json(run / "result.json")
            session = _load_json(run / 'session.json')
            capture = _load_json(run / 'capture.json')
            sequence = _load_json(run / 'sequence.json')
            recovery = _load_json(run / 'recovery.json')
            confirmed = (recovery.get('source') == 'operator_external_check'
                         and recovery.get('xy_confirmed') is True
                         and recovery.get('af_z_confirmed') is True
                         and bool(recovery.get('confirmed_at')))
            recovery_required = status.get('recovery_required') is not False and not confirmed
            if status.get('state') not in ('complete', 'stopped', 'failed') and not confirmed:
                recovery_required = True
            kind = status.get("kind") or ("mark_capture" if recipe.get("kind") == "mark_capture"
                                          else "hardware" if status.get("kind") == "hardware_test"
                                          else "tilt" if recipe.get("point_order") is not None
                                          or "af_timeout_s" in recipe else "unknown")
            if recipe.get('kind') == 'sequence':
                kind='sequence'
                if not result:result=_load_json(run/'angle_result.json')
            if kind == "mark_capture":
                kind = "mark"
            elif kind == "hardware_test":
                kind = "hardware"
            runs.append({
                "path": run, "run_id": run.name, "kind": kind, "simulated": simulated,
                "started": status.get("started_at", ""), "state": status.get("state", "unknown"),
                "valid": bool(status.get('result_valid')), 'recovery': recovery_required,
                'capture_complete': kind == 'mark' and status.get('state') == 'complete',
                'pair_complete': bool(capture.get('status', {}).get('pair_complete')),
                "stage": status.get("failure_stage", ""), "recipe": recipe, "result": result,
                "session": session, "n_images": (len(sequence.get('images',[])) + sum(s.get('planes',0) for s in sequence.get('stacks',[])) + sum(len(_load_json(Path(c['record_path'])/'capture.json').get('images',{})) for c in sequence.get('captures',[]) if c.get('record_path'))
                                                  if kind=='sequence' else len(capture.get('images', {}))),
                'sequence':sequence,
                'capture': capture,
            })
    return runs


def index_rows(runs):
    for run in runs:
        recipe, result, session = run["recipe"], run["result"], run["session"]
        yield {
            "run_id": run["run_id"], "started_at": run["started"], "kind": run["kind"],
            "simulated": run["simulated"], "sample": session.get("sample", ""),
            "operator": session.get("operator", ""),
            "recipe": recipe.get("recipe_name") or recipe.get("kind", ""),
            "experiment": recipe.get("experiment_name") or "",
            "state": run['state'], 'result_valid': run['valid'],
            'capture_complete': run['capture_complete'], 'pair_complete': run['pair_complete'],
            "recovery_required": run["recovery"],
            "rx_urad": result.get("rx_urad", ""), "ry_urad": result.get("ry_urad", ""),
            "rms_um": "" if "rms_m" not in result else result["rms_m"] * 1e6,
            "pv_um": "" if "pv_m" not in result else result["pv_m"] * 1e6,
            "n_points": len(recipe.get("point_order", ())) if recipe else "",
            "n_images": run["n_images"] or "",
            "failure_stage": run["stage"], "path": str(run["path"]),
        }


class RecordsPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.runs = []
        lay, head = _page(self, "结果与追溯",
                          "每次运行自动归档 · 可筛选、对比、导出 · 无需连接设备")
        self.export_button = QPushButton("导出所选为 CSV", clicked=self.export_csv)
        self.index_button = QPushButton("重建 index.csv", clicked=self.rebuild_index)
        bundle = QPushButton("导出诊断包…", clicked=app.export_diagnostic_bundle)
        bundle.setObjectName("primary")
        for button in (self.export_button, self.index_button, bundle):
            head.addWidget(button)

        filters = QWidget()
        filters.setObjectName("bar")
        fl = hbox(filters, spacing=9, margins=(12, 8, 12, 8))
        self.kind_combo = QComboBox()
        self.kind_combo.addItems(["全部", "测角", "采图", "硬件单步", "自动任务"])
        self.kind_combo.setFixedWidth(96)
        fl.addWidget(self.kind_combo)
        self.state_combo = QComboBox()
        self.state_combo.addItems(['状态：全部', '已完成', '中止', '失败', '待恢复'])
        self.state_combo.setFixedWidth(110)
        fl.addWidget(self.state_combo)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索 run_id / 配方 / 实验 / 样品…")
        fl.addWidget(self.search, 1)
        self.real_only = QComboBox()
        self.real_only.addItems(["现场 + 离线演示", "仅现场设备", "仅离线演示"])
        self.real_only.setFixedWidth(140)
        fl.addWidget(self.real_only)
        fl.addWidget(QPushButton("刷新", clicked=self.reload))
        lay.addWidget(filters)
        for control in (self.kind_combo, self.state_combo, self.real_only):
            control.currentIndexChanged.connect(lambda *_: self._populate())
        self.search.textChanged.connect(lambda *_: self._populate())

        body = QWidget()
        bl = hbox(body, spacing=11, margins=0)
        lay.addWidget(body, 1)
        table_card = Card()
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ("时间", "类型", "样品", "配方 / 实验", "Rx µrad", "Ry µrad", "RMS µm", "状态"))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.itemSelectionChanged.connect(self._show_detail)
        table_card.add(self.table)
        bl.addWidget(table_card, 2)

        side = QWidget()
        sl = vbox(side, spacing=11, margins=0)
        side.setFixedWidth(360)
        bl.addWidget(side)
        self.detail_title = label("", role="muted")
        detail_card = Card("所选记录", trailing=self.detail_title)
        # Outcome first, numbers second, provenance last and folded away.
        self.detail_verdict = label("选择一条记录查看结论。", wrap=True)
        detail_card.add(self.detail_verdict)
        self.detail_metrics = label("", wrap=True, mono=True)
        self.detail_metrics.setStyleSheet("font-size:14px; font-weight:600")
        self.detail_metrics.hide()
        detail_card.add(self.detail_metrics)
        self.detail = label("—", wrap=True)
        detail_card.add(self.detail)
        self.more_button = QPushButton("显示路径与内部字段")
        self.more_button.setObjectName("flat")
        self.more_button.setCheckable(True)
        self.more_button.toggled.connect(self._toggle_more)
        detail_card.add(self.more_button)
        self.detail_more = label("", wrap=True, mono=True)
        self.detail_more.setStyleSheet("font-size:10.5px")
        self.detail_more.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail_more.hide()
        detail_card.add(self.detail_more)
        buttons = QWidget()
        btl = hbox(buttons, spacing=6, margins=0)
        btl.addWidget(QPushButton("打开目录", clicked=self.open_dir))
        btl.addWidget(QPushButton("复制路径", clicked=self.copy_path))
        btl.addStretch(1)
        detail_card.add(buttons)
        sl.addWidget(detail_card)

        self.index_path = label("汇总索引 index.csv（Excel 可直接打开）", role="muted", wrap=True)
        self.index_path.setToolTip(str(Path(app.data_root) / "index.csv"))
        sl.addWidget(self.index_path)
        sl.addStretch(1)
        self.capture_selector=QComboBox();self.capture_selector.setToolTip('选择孔位图像');self.capture_selector.currentIndexChanged.connect(self._show_detail)
        self._preview_run_id=None
        lay.addWidget(self.capture_selector)
        self.evidence_tabs=QTabWidget()
        image_page=QWidget();il=hbox(image_page,margins=0);self.image_labels=[]
        for title in ('上孔','下孔'):
            col=QWidget();cl=vbox(col,margins=0);cl.addWidget(label(title,role='muted'));view=label('选择一条含图像的记录');view.setMinimumSize(160,120);view.setAlignment(Qt.AlignCenter);cl.addWidget(view);il.addWidget(col);self.image_labels.append(view)
        self.evidence_tabs.addTab(image_page,'上下孔图像')
        self.evidence_text={}
        for key,title in [('sequence.json','原始记录 · 执行'),('snapshot.json','原始记录 · 配方快照'),('command_log.json','原始记录 · 动作与诊断')]:
            text=QPlainTextEdit();text.setReadOnly(True);self.evidence_text[key]=text;self.evidence_tabs.addTab(text,title)
        lay.addWidget(self.evidence_tabs)
        self.evidence_tabs.setMaximumHeight(240)
        self.reload()

    # ---- data ----

    def reload(self):
        self.runs = scan_runs(self.app.data_root)
        self._populate()

    def on_show(self):
        self.reload()
        if self.table.rowCount():self.table.selectRow(0)

    def _filtered(self):
        kind_map = {1: "tilt", 2: "mark", 3: "hardware", 4: "sequence"}
        kind = kind_map.get(self.kind_combo.currentIndex())
        state_idx = self.state_combo.currentIndex()
        real = self.real_only.currentIndex()
        needle = self.search.text().strip().lower()
        for run in self.runs:
            if kind and run["kind"] != kind:
                continue
            if state_idx == 1 and (run['state'] != 'complete' or run['recovery']):
                continue
            if state_idx == 2 and run["state"] != "stopped":
                continue
            if state_idx == 3 and run["state"] != "failed":
                continue
            if state_idx == 4 and not run["recovery"]:
                continue
            if real == 1 and run["simulated"]:
                continue
            if real == 2 and not run["simulated"]:
                continue
            if needle:
                hay = " ".join(str(v) for v in (
                    run["run_id"], run["recipe"].get("recipe_name", ""),
                    run["recipe"].get("experiment_name", ""), run["session"].get("sample", ""),run.get("sequence",{}).get("recipe_identity",{}))).lower()
                if needle not in hay:
                    continue
            yield run

    def _populate(self):
        runs = list(self._filtered())
        self.table.setRowCount(len(runs))
        for row, run in enumerate(runs):
            result = run["result"]
            values = (
                run["started"][5:16].replace("T", " ") if run["started"] else run["run_id"][:12],
                KIND_NAMES.get(run["kind"], run["kind"]),
                run["session"].get("sample", "—") or "—",
                run["recipe"].get("recipe_name") or run["recipe"].get("experiment_name")
                or run["recipe"].get("kind", "—"),
                "" if "rx_urad" not in result else f"{result['rx_urad']:+.2f}",
                "" if "ry_urad" not in result else f"{result['ry_urad']:+.2f}",
                "" if "rms_m" not in result else f"{result['rms_m']*1e6:.2f}",
                STATE_NAMES.get(run["state"], run["state"]) + ("·待恢复" if run["recovery"] else "") + ("·演示" if run["simulated"] else ""),
            )
            for col, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                item.setTextAlignment(Qt.AlignCenter if col != 3 else Qt.AlignLeft)
                if run["state"] == "failed":
                    item.setForeground(self._color(theme.BAD))
                elif run["valid"]:
                    item.setForeground(self._color(theme.OK))
                elif run["recovery"]:
                    item.setForeground(self._color(theme.WARN))
                self.table.setItem(row, col, item)
        self._populate_runs = runs

    def _color(self, hex_color):
        from PySide6.QtGui import QColor
        return QColor(hex_color)

    def _selected_run(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        runs = getattr(self, "_populate_runs", [])
        if len(rows) != 1:
            return None
        row = rows.pop()
        return runs[row] if 0 <= row < len(runs) else None

    def _toggle_more(self, shown):
        self.detail_more.setVisible(shown)
        self.more_button.setText("收起路径与内部字段" if shown else "显示路径与内部字段")

    def _show_detail(self):
        from html import escape

        run = self._selected_run()
        if run is None:
            return
        self.detail_title.setText(KIND_NAMES.get(run["kind"], run["kind"]))
        recipe, result, session = run["recipe"], run["result"], run["session"]
        sequence = run.get("sequence", {})
        identity = sequence.get("recipe_identity", {})
        kind, headline = run_verdict(run)
        bg, ink = VERDICT_COLORS[kind]
        source = (
            f'<div style="font-weight:700;color:{theme.WARN}">离线演示结果 · 非现场数据</div>'
            if run["simulated"]
            else f'<div style="color:{theme.MUTED}">现场设备记录</div>'
        )
        self.detail_verdict.setText(
            source
            + f'<div style="font-size:17px;font-weight:700;color:{ink}">{escape(headline)}</div>'
        )
        self.detail_verdict.setStyleSheet(
            f"background:{bg}; border-radius:8px; padding:8px 10px;"
        )
        self.detail_metrics.setVisible(bool(result))
        if result:
            self.detail_metrics.setText(
                f"Rx  {result.get('rx_urad', 0):+.3f} µrad\n"
                f"Ry  {result.get('ry_urad', 0):+.3f} µrad\n"
                f"RMS {result.get('rms_m', 0)*1e6:.3f} µm · PV {result.get('pv_m', 0)*1e6:.3f} µm"
            )
        lines = []
        name = recipe.get("recipe_name") or recipe.get("experiment_name")
        if name:
            revision = f" · Rev.{identity.get('revision', 0):03d}" if identity else ""
            lines.append(f"配方：{name}{revision}")
        if session.get("sample") or session.get("operator"):
            lines.append(f"样品：{session.get('sample') or '—'} · 操作员：{session.get('operator') or '—'}")
        if run["started"]:
            lines.append("开始：" + run["started"][:19].replace("T", " "))
        if run["kind"] == "mark":
            lines.append(
                f"图像：{run['n_images']} 张 · "
                + ("上下孔成对" if run["pair_complete"] else "图片对不完整")
            )
            lines.append("图像内容 / 清晰度 / 标定需另行核验，不等同于对位分析通过。")
        elif run["kind"] == "sequence":
            done = sum(c.get("state") == "complete" for c in sequence.get("captures", []))
            lines.append(f"图像：{run['n_images']} 张 · 孔位采图完成 {done} 组")
            lines.append("自动任务完成不等于 SOMA 对位分析通过。")
        elif run["n_images"]:
            lines.append(f"图像：{run['n_images']} 张")
        if recipe.get("point_order"):
            lines.append(f"点位：{len(recipe['point_order'])} 个")
        if run["recovery"]:
            lines.append("待恢复：上次动作未确认完成，请到运行中心处理。")
        self.detail.setText("\n".join(lines) or "—")
        more = [f"run_id  {run['run_id']}", f"目录  {run['path']}"]
        if sequence.get("output_directory"):
            more.append(f"图像目录  {sequence['output_directory']}")
        if identity:
            more.append(f"配方 ID  {identity.get('recipe_id')}")
            more.append(f"执行模式  {identity.get('execution_mode')}")
        if sequence.get("disposition"):
            more.append(f"disposition  {sequence['disposition']}")
        more.append(f"state  {run['state']} · recovery_required  {run['recovery']}")
        if recipe.get("experiment_name"):
            more.append(f"实验  {recipe['experiment_name']}")
        self.detail_more.setText("\n".join(more))
        if hasattr(self,'evidence_text'):
            for key,edit in self.evidence_text.items():edit.setPlainText(json.dumps(_load_json(run['path']/key),ensure_ascii=False,indent=2))
            captures=run.get('sequence',{}).get('captures',[])
            if self._preview_run_id!=run['run_id']:
                self.capture_selector.blockSignals(True);self.capture_selector.clear()
                for capture in captures:self.capture_selector.addItem(capture['station'],capture['output_directory'])
                pairs={}
                for image in run.get('sequence',{}).get('images',[]):
                    key=image.get('context','')+image.get('pair_id','mark')
                    pairs.setdefault(key,{})[image['layer']]=image['path']
                for key,paths in pairs.items():self.capture_selector.addItem(key,paths)
                self.capture_selector.blockSignals(False);self._preview_run_id=run['run_id']
            selected_output=self.capture_selector.currentData()
            output=Path(selected_output) if selected_output and not isinstance(selected_output,dict) else Path(run.get('capture',{}).get('status',{}).get('output_directory',str(run['path'])))
            for view,name in zip(self.image_labels,('upper.tif','lower.tif')):
                file=Path(selected_output.get(name.split('.')[0],str(output/'missing'))) if isinstance(selected_output,dict) else output/name
                try:
                    import numpy as np
                    import tifffile
                    data=tifffile.imread(file)
                    if data.ndim!=2:raise ValueError('仅预览二维单通道 TIFF')
                    lo,hi=np.percentile(data,[1,99]);display=np.ascontiguousarray(np.clip((data.astype(float)-lo)/max(hi-lo,1)*255,0,255).astype('uint8'))
                    image=QImage(display.data,display.shape[1],display.shape[0],display.strides[0],QImage.Format_Grayscale8).copy()
                    view.setPixmap(QPixmap.fromImage(image).scaled(280,150,Qt.KeepAspectRatio,Qt.SmoothTransformation));view.setToolTip(str(file)+' · 仅预览拉伸，原始文件不变')
                except (OSError,ValueError,ImportError):view.setText('本条记录无可预览图像')


    def open_dir(self):
        run = self._selected_run()
        if run and hasattr(os, "startfile"):
            os.startfile(run["path"])

    def copy_path(self):
        run = self._selected_run()
        if run:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(str(run["path"]))

    def rebuild_index(self):
        self.runs = scan_runs(self.app.data_root)
        rows = list(index_rows(self.runs))
        self.app.data_root.mkdir(parents=True, exist_ok=True)
        path = Path(self.app.data_root) / "index.csv"
        try:
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()) if rows else
                                        ["run_id", "started_at", "kind", "simulated", "sample",
                                         "operator", "recipe", "experiment", "state", "result_valid",
                                         "capture_complete", "pair_complete",
                                         "recovery_required", "rx_urad", "ry_urad", "rms_um",
                                         "pv_um", "n_points", "n_images", "failure_stage", "path"])
                writer.writeheader()
                writer.writerows(rows)
                stream.flush()
        except OSError as exc:
            QMessageBox.critical(self, "索引写入失败", str(exc))
            return
        self.app.log("ok", f"index.csv 已重建：{len(rows)} 条记录 → {path}")

    def export_csv(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        runs = getattr(self, "_populate_runs", [])
        selected = [runs[i] for i in sorted(rows) if 0 <= i < len(runs)] or runs
        if not selected:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", "runs.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                data = list(index_rows(selected))
                writer = csv.DictWriter(stream, fieldnames=list(data[0].keys()))
                writer.writeheader()
                writer.writerows(data)
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.app.log("ok", f"已导出 {len(selected)} 条记录 → {path}")
