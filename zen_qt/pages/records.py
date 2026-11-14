"""记录与分析 page: run archive browser + index.csv export + trend sparkline."""

import csv
import json
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QWidget,
)

from .. import theme
from ..widgets import Card, hbox, label, vbox
from .device import _page

KIND_NAMES = {"tilt": "测角", "mark": "采图", "hardware": "硬件单步", "unknown": "记录"}
STATE_PILLS = {"complete": ("ok", "有效"), "stopped": ("warn", "中止"),
               "failed": ("bad", "失败"), "running": ("accent", "运行中")}


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
                "session": session, "n_images": len(capture.get('images', {})),
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
        lay, head = _page(self, "记录与分析",
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
        self.kind_combo.addItems(["全部", "测角", "采图", "硬件单步"])
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
        self.real_only.addItems(["真实 + 演示", "仅真实设备", "仅演示"])
        self.real_only.setFixedWidth(110)
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
        self.detail = label("—", wrap=True, mono=True)
        self.detail.setStyleSheet("font-size:11px; line-height:1.6")
        detail_card.add(self.detail)
        buttons = QWidget()
        btl = hbox(buttons, spacing=6, margins=0)
        btl.addWidget(QPushButton("打开目录", clicked=self.open_dir))
        btl.addWidget(QPushButton("复制路径", clicked=self.copy_path))
        btl.addStretch(1)
        detail_card.add(buttons)
        sl.addWidget(detail_card)

        index_card = Card("汇总索引", trailing=label("分析入口", role="muted"))
        index_card.add(label("所有运行自动汇总成一张宽表，Excel / pandas 直接打开。",
                             role="muted", wrap=True))
        self.index_path = label(str(Path(app.data_root) / "index.csv"), mono=True, wrap=True)
        self.index_path.setStyleSheet("font-size:10.5px; background:#f5f7fa; border:1px solid "
                                      f"{theme.LINE}; border-radius:7px; padding:8px 10px;")
        index_card.add(self.index_path)
        sl.addWidget(index_card)
        sl.addStretch(1)
        self.reload()

    # ---- data ----

    def reload(self):
        self.runs = scan_runs(self.app.data_root)
        self._populate()

    def on_show(self):
        self.reload()

    def _filtered(self):
        kind_map = {1: "tilt", 2: "mark", 3: "hardware"}
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
                    run["recipe"].get("experiment_name", ""), run["session"].get("sample", ""))).lower()
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
                run["state"] + ("·待恢复" if run["recovery"] else "") + ("·演示" if run["simulated"] else ""),
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

    def _show_detail(self):
        run = self._selected_run()
        if run is None:
            return
        self.detail_title.setText(run["run_id"][:24])
        recipe, result, session = run["recipe"], run["result"], run["session"]
        lines = [
            f"类型 {KIND_NAMES.get(run['kind'], run['kind'])} · {'演示' if run['simulated'] else '真实'}",
            f"开始 {run['started']}",
            f"状态 {run['state']} · 待恢复 {run['recovery']}",
        ]
        if run['kind'] == 'mark':
            lines.append(f"图像 {run['n_images']} 张 · 保存完成 {run['capture_complete']} · 图片对完整 {run['pair_complete']}")
            lines.append('图像内容/清晰度/标定需另行核验；不等同于对位分析通过。')
        elif run['kind'] == 'tilt':
            lines.append(f"测角结果有效 {run['valid']}")
        if recipe.get("recipe_name"):
            lines.append(f"配方 {recipe['recipe_name']} · 实验 {recipe.get('experiment_name', '')}")
        if recipe.get("point_order"):
            lines.append(f"点位 {len(recipe['point_order'])} 个")
        if run["n_images"]:
            lines.append(f"图像 {run['n_images']} 张")
        if result:
            lines.append(f"Rx {result.get('rx_urad', 0):+.3f} µrad · Ry {result.get('ry_urad', 0):+.3f} µrad")
            lines.append(f"RMS {result.get('rms_m', 0)*1e6:.3f} µm · PV {result.get('pv_m', 0)*1e6:.3f} µm")
        if session.get("sample") or session.get("operator"):
            lines.append(f"样品 {session.get('sample', '—')} · 操作员 {session.get('operator', '—')}")
        if run["stage"]:
            lines.append(f"失败阶段 {run['stage']}")
        lines.append("")
        lines.append(str(run["path"]))
        self.detail.setText("\n".join(lines))

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
