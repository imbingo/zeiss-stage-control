"""Chinese captions for Qt's own dialog buttons.

The Windows runtime ships without Qt's translation files (trim_runtime.py
removes them), so standard buttons would otherwise read Save / Discard / Cancel.
"""

from PySide6.QtCore import QCoreApplication, QTranslator

BUTTONS = {
    "OK": "确定",
    "&OK": "确定",
    "Cancel": "取消",
    "&Cancel": "取消",
    "Save": "保存",
    "&Save": "保存",
    "Save All": "全部保存",
    "Discard": "不保存",
    "Don't Save": "不保存",
    "Open": "打开",
    "&Open": "打开",
    "Yes": "是",
    "&Yes": "是",
    "Yes to &All": "全部是",
    "No": "否",
    "&No": "否",
    "N&o to All": "全部否",
    "Abort": "中止",
    "Retry": "重试",
    "Ignore": "忽略",
    "Close": "关闭",
    "&Close": "关闭",
    "Apply": "应用",
    "Reset": "重置",
    "Help": "帮助",
    "Restore Defaults": "恢复默认",
}
CONTEXTS = {"QPlatformTheme", "QDialogButtonBox", "QMessageBox", "QInputDialog"}


class ButtonTranslator(QTranslator):
    def isEmpty(self):
        return False

    def translate(self, context, source, disambiguation=None, n=-1):
        return BUTTONS.get(source, "") if context in CONTEXTS else ""


def install():
    app = QCoreApplication.instance()
    if app is None or getattr(app, "_zen_button_translator", None):
        return
    app._zen_button_translator = ButtonTranslator(app)
    app.installTranslator(app._zen_button_translator)
