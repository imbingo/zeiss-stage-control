"""Small helpers for one-off calls that must not block the UI thread.

Device calls inside a Workflow are already serialised by its task gate; the
helpers here are for *read-only* side queries (live position readout,
preflight) which must only be issued while the workflow gate is free.
"""

from queue import SimpleQueue
from threading import Thread

from PySide6.QtCore import QObject, QTimer, Signal


class CallBridge(QObject):
    """Re-enters the GUI thread with results from any python thread."""

    done = Signal(str, object)
    fail = Signal(str, object)
    progress = Signal(str, object)


_BRIDGE = None


def bridge(parent):
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = CallBridge(parent)
        _BRIDGE.moveToThread(parent.thread())
    return _BRIDGE


def run(tag, fn, *, report=None):
    """Run `fn` on a daemon thread; deliver results via the shared bridge."""
    def worker():
        try:
            value = fn()
        except Exception as error:  # noqa: BLE001 — surfaced to the UI log
            bridge_signal().fail.emit(tag, error)
        else:
            bridge_signal().done.emit(tag, value)

    def bridge_signal():
        return _BRIDGE

    Thread(target=worker, daemon=True).start()


def run_reporting(tag, fn):
    """Run `fn(report)`; `report(key, value)` emits progress from the thread."""
    def worker():
        def report(key, value):
            _BRIDGE.progress.emit(tag, (key, value))
        try:
            value = fn(report)
        except Exception as error:  # noqa: BLE001
            _BRIDGE.fail.emit(tag, error)
        else:
            _BRIDGE.done.emit(tag, value)

    Thread(target=worker, daemon=True).start()


class EventPump:
    """Drains a Workflow's SimpleQueue on a QTimer (replaces root.after)."""

    def __init__(self, parent, handler, interval_ms=35):
        self.handler = handler
        self.timer = QTimer(parent)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def tick(self):
        self.handler()
