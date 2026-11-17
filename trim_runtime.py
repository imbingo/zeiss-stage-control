"""Remove files the shipped runtime never loads. Usage: trim_runtime.py <runtime dir>"""

import shutil
import sys
from pathlib import Path

ROOT_DIRS = ("Doc", "include", "libs", "Scripts", "Tools")
LIB_DIRS = ("test", "idlelib", "ensurepip", "venv", "turtledemo", "lib2to3", "pydoc_data")
SITE_PACKAGES = ("pip", "setuptools", "pkg_resources", "_distutils_hack", "grpc_tools", "pipx", "wheel")
QT_KEEP_MODULES = {"QtCore", "QtGui", "QtWidgets"}
QT_DIRS = ("translations", "qml", "metatypes", "resources", "include", "typesystems", "glue", "scripts", "support")
QT_DLL_PREFIXES = ("Qt6Quick", "Qt6Qml", "Qt6Designer", "Qt6Help", "Qt6Sql", "Qt6Test", "Qt6Pdf",
                   "Qt6OpenGL", "Qt6Network", "Qt6Concurrent", "Qt6Xml", "Qt6DBus", "Qt6PrintSupport",
                   "Qt6UiTools", "Qt6Labs", "Qt6Lottie", "Qt6ShaderTools", "Qt6SvgWidgets")
QT_PLUGIN_KEEP = {"platforms", "styles", "imageformats", "iconengines"}


def remove(path):
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def main(runtime):
    runtime = Path(runtime)
    site = runtime / "Lib" / "site-packages"
    if not site.is_dir():
        raise SystemExit(f"not a Python runtime: {runtime}")
    before = sum(f.stat().st_size for f in runtime.rglob("*") if f.is_file())
    for name in ROOT_DIRS:
        remove(runtime / name)
    for path in (*runtime.glob("python-*.exe"), runtime / "NEWS.txt"):
        remove(path)
    for name in LIB_DIRS:
        remove(runtime / "Lib" / name)
    for name in SITE_PACKAGES:
        remove(site / name)
        for info in site.glob(f"{name}-*.dist-info"):
            remove(info)
    # setuptools is removed above; its startup hook must not reference it.
    if not (site / "_distutils_hack").exists():
        remove(site / "distutils-precedence.pth")
    qt = site / "PySide6"
    if qt.is_dir():
        for name in QT_DIRS:
            remove(qt / name)
        for path in qt.iterdir():
            if path.suffix == ".exe" or path.name == "opengl32sw.dll":
                remove(path)
            elif path.suffix in (".pyd", ".pyi") and path.stem.startswith("Qt") \
                    and path.stem.split(".")[0] not in QT_KEEP_MODULES:
                remove(path)
            elif path.suffix == ".dll" and path.name.startswith(QT_DLL_PREFIXES):
                remove(path)
        plugins = qt / "plugins"
        if plugins.is_dir():
            for path in plugins.iterdir():
                if path.name not in QT_PLUGIN_KEEP:
                    remove(path)
    for cache in list(runtime.rglob("__pycache__")):
        remove(cache)
    after = sum(f.stat().st_size for f in runtime.rglob("*") if f.is_file())
    print(f"trimmed runtime: {before / 1048576:.0f} MB -> {after / 1048576:.0f} MB")


if __name__ == "__main__":
    main(sys.argv[1])
