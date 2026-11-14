"""Capture real Qt widgets against simulated hardware; never connects to Gateway."""

import os, tempfile, time, json
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics
from PySide6.QtCore import Qt
import matplotlib

matplotlib.rcParams["font.sans-serif"] = ["Noto Sans CJK JP", "DejaVu Sans"]
QFont.insertSubstitution("Microsoft YaHei UI", "Noto Sans CJK SC")
from zen_qt.app import WorkbenchWindow


def main():
    app = QApplication([])
    font_path = os.environ.get("ZEN_CAPTURE_FONT")
    if font_path:
        font_id = QFontDatabase.addApplicationFont(font_path)
        families = QFontDatabase.applicationFontFamilies(font_id)
        if not families:
            raise RuntimeError("Capture font could not be loaded")
        QFont.insertSubstitution("Microsoft YaHei UI", families[0])
        app.setFont(QFont(families[0], 10))
        from matplotlib import font_manager

        font_manager.fontManager.addfont(font_path)
        matplotlib.rcParams["font.sans-serif"] = [
            font_manager.FontProperties(fname=font_path).get_name()
        ]
    if not QFontMetrics(app.font()).inFontUcs4(ord("测")):
        raise RuntimeError("No Chinese font available: screenshots would be unreadable")
    window = WorkbenchWindow(Path(tempfile.mkdtemp(prefix="workbench-ui-")) / "data")
    window.resize(1600, 1000)
    window.show()
    output = Path(
        os.environ.get("ZEN_CAPTURE_DIR", str(Path(__file__).parent / "screenshots"))
    )
    output.mkdir(parents=True, exist_ok=True)

    def tick():
        app.processEvents()
        time.sleep(0.02)
        app.processEvents()

    def wait(test):
        end = time.monotonic() + 40
        while time.monotonic() < end:
            tick()
            if test():
                return
        raise RuntimeError("Timed out")

    def snap(name):
        tick()
        assert window.grab().save(str(output / name))

    window.operator_edit.setText("工程师 · 王工")
    window.sample_edit.setText("W-001 · LOT-A")
    p = window.pages["recipes"]
    p.seed_demo()
    p.mark_dirty()
    assert p.save_new_version()
    window.connect_demo("sequence")
    wait(lambda: not window.workflow.busy)
    window.z_timer.stop()
    center = window.pages["sequence"]
    center.select_recipe(p.current_id, 1, mode="simulation")
    if not window.dock.collapsed:
        window.dock.toggle_fold()
    snap("01-run-center-ready.png")
    center.preflight()
    wait(lambda: not window.workflow.busy)
    center.path_confirm.setChecked(True)
    center.load_confirm.setChecked(True)
    center.start()
    wait(lambda: not window.workflow.busy)
    tick()
    assert (
        window.workflow.sequence_report["state"] == "complete"
    ), window.workflow.sequence_report
    center.tabs.setCurrentIndex(0)
    tick()
    snap("02-angle-path-and-z.png")
    center.tabs.setCurrentIndex(1)
    tick()
    snap("03-upper-lower-mark.png")
    window.navigate("recipes")

    def find(node, sid):
        for i in range(node.childCount()):
            child = node.child(i)
            value = child.data(0, Qt.UserRole)
            if value and value[0] == sid:
                return child
            result = find(child, sid)
            if result:
                return result

    p.library_tabs.setCurrentIndex(1)
    upper = next(n for n in p._body['program_steps'] if n['kind']=='capture_upper')
    p.step_tree.setCurrentItem(find(p.step_tree.invisibleRootItem(), upper['step_id']))
    snap("04-recipe-mark-parameters.png")
    angle = next(n for n in p._body['program_steps'] if n['kind']=='measure_angle')
    p.step_tree.setCurrentItem(find(p.step_tree.invisibleRootItem(), angle['step_id']))
    snap("05-recipe-angle-parameters.png")
    window.navigate("device")
    window.equipment_tabs.setCurrentIndex(1)
    window.pages["manual"].read()
    wait(lambda: not window.workflow.busy)
    snap("05-manual.png")
    window.navigate("hardware")
    snap("06-hardware.png")
    window.navigate("records")
    window.pages["records"].table.selectRow(0)
    snap("07-results.png")
    window.navigate("settings")
    snap("08-settings.png")
    window.navigate("sequence")
    window.resize(1366, 768)
    tick()
    snap("09-small-screen.png")
    assert window.width() == 1366 and window.height() == 768, (
        window.width(),
        window.height(),
    )
    (output.parent / "simulation.json").write_text(
        json.dumps(
            {
                "simulated": True,
                "state": window.workflow.sequence_report["state"],
                "captures": len(window.workflow.sequence_report["images"]),
                "recipe_identity": window.workflow.sequence_report["recipe_identity"],
                "final_xy_m": window.workflow.device.xy,
                "final_z_m": window.workflow.device.z,
                "small_window": [window.width(), window.height()],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    p._dirty = False
    window.close()


if __name__ == "__main__":
    main()
