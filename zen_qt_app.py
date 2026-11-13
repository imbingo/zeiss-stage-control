"""Qt entry point: python zen_qt_app.py"""

import os
import sys
from pathlib import Path


def main():
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    from PySide6.QtWidgets import QApplication
    from zen_qt.app import WorkbenchWindow

    app = QApplication(sys.argv)
    app.setApplicationName("ZEN Surface Tilt Workbench")
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ZENSurfaceTilt"
    window = WorkbenchWindow(root)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
