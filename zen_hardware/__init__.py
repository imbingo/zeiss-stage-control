"""Device boundary shared by the Windows workbench and simulator."""

from enum import Enum
from typing import Protocol


class Motion(Enum):
    IDLE = "idle"
    MOVING = "moving"
    UNKNOWN = "unknown"
    ERROR = "error"


class Device(Protocol):
    """Positions at this boundary use SI metres, as in the Gateway API."""

    def connect(self) -> None: ...
    def read_xy(self) -> tuple[float, float]: ...
    def motion_state(self) -> Motion: ...
    def move_xy(self, x: float, y: float) -> None: ...
    def load_experiment(self, name: str) -> None: ...
    def autofocus(self, timeout_seconds: float) -> float: ...


class DeviceDisconnected(ConnectionError):
    pass


class MoveFailed(RuntimeError):
    pass


class AutofocusTimeout(TimeoutError):
    pass
