"""Clicks stay available; serialize actions behind reads without losing intent."""

from time import monotonic
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def action_busy(app):
    w = app.workflow
    return bool(w and w.busy and getattr(w, "task_kind", "action") != "read_only")


def cancel_pending(owner):
    owner._pending_click = None
    if hasattr(owner, "_auto_start_token"):
        owner._auto_start_token = None


def defer_read(app, owner, callback, token, notice):
    w = app.workflow
    if not w or not w.busy:
        owner._pending_click = None
        return False
    if action_busy(app):
        cancel_pending(owner)
        notice("设备任务正在执行，不能叠加下发动作。请等待结束，或先点击“停止”。")
        return True
    try:
        stamp = token()
    except Exception:
        stamp = None
    pending = getattr(owner, "_pending_click", None)
    if pending and pending["stamp"] == stamp:
        notice("已收到请求，正在等待读取结束；无需反复点击。")
        return True
    pending = {
        "stamp": stamp,
        "workflow": w,
        "generation": getattr(w, "action_generation", 0),
        "deadline": monotonic() + 30,
        "visible": owner.isVisible(),
    }
    owner._pending_click = pending
    notice("正在读取设备状态，读取结束后继续本次操作；可点击“停止”取消。")

    def poll():
        if getattr(owner, "_pending_click", None) is not pending:
            return
        reason = None
        try:
            if app.workflow is not w or token() != stamp:
                reason = "连接或参数已变化，本次请求已取消，请重新点击。"
            elif QApplication.activeModalWidget() is not None:
                reason = (
                    "参数对话框已打开，本次待执行请求已取消，请编辑完成后重新点击。"
                )
            elif pending["visible"] and not owner.isVisible():
                reason = "已离开当前页面，本次待执行请求已取消。"
            elif w._stop.is_set():
                reason = "已取消本次待执行操作。"
            elif getattr(w, "action_generation", 0) != pending["generation"]:
                reason = "其他设备任务已开始，本次请求已取消。"
            elif monotonic() > pending["deadline"]:
                reason = "设备读取仍未结束，未发送动作。请检查通信或点击“停止”。"
        except Exception:
            reason = "页面参数已变化，请重新点击。"
        if reason:
            cancel_pending(owner)
            notice(reason)
            return
        if w.busy:
            QTimer.singleShot(60, poll)
            return
        owner._pending_click = None
        callback()

    QTimer.singleShot(60, poll)
    return True
