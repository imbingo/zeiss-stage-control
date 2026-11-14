"""Capture real Qt widgets against simulated hardware; never connects to Gateway."""

import os, tempfile, time, json
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt
import matplotlib

matplotlib.rcParams["font.sans-serif"] = ["Noto Sans CJK JP", "DejaVu Sans"]
QFont.insertSubstitution("Microsoft YaHei UI", "Noto Sans CJK SC")
from zen_qt.app import WorkbenchWindow


def main():
    app = QApplication([])
    window = WorkbenchWindow(Path(tempfile.mkdtemp(prefix="workbench-ui-")) / "data")
    window.resize(1600, 1000)
    window.show()
    output = Path(os.environ.get("ZEN_CAPTURE_DIR",str(Path(__file__).parent / "screenshots")))
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
    group = p._body["step_tree"][4]
    second = json.loads(json.dumps(group["children"][0]))
    second.update(step_id="H02", label="H02 上下孔采图")
    second["params"].update(x_um=4500, y_um=1500, upper_z_um=102, lower_z_um=202)
    group["children"].append(second)
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
    snap("02-run-center-complete.png")
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

    p.step_tree.setCurrentItem(find(p.step_tree.invisibleRootItem(), "H01"))
    snap("03-recipe-hole.png")
    p.step_tree.setCurrentItem(find(p.step_tree.invisibleRootItem(), "S1"))
    snap("04-recipe-tilt.png")
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
                "captures": len(window.workflow.sequence_report["captures"]),
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
