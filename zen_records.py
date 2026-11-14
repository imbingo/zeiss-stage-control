"""Persist a run incrementally using an explicit, credential-free schema."""

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from zen_acquisition import PointRecord


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class RunRecords:
    columns = (
        "index", "target_x_m", "target_y_m", "before_af_x_m", "before_af_y_m",
        "after_af_x_m", "after_af_y_m", "af_z_m", "status", "failure_stage",
    )

    def __init__(
        self, root: Path, *, name, experiment, targets, tolerance_m, af_timeout,
        arrival_timeout, sign_convention, simulated, confirmed_start=None,
    ):
        if pending_recovery(root, simulated=simulated):
            raise RuntimeError("存在未确认的设备状态，请先完成恢复确认")
        directory = root / ("demo" if simulated else "field")
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / uuid4().hex
        self.path.mkdir()
        self.started = utc_now()
        write_json(self.path / "recipe.json", {
            "schema_version": 1, "software_version": "2.6.0",
            "recipe_name": name, "experiment_name": experiment,
            "point_order": targets, "xy_tolerance_m": tolerance_m,
            "af_timeout_s": af_timeout, "arrival_timeout_s": arrival_timeout,
            "coordinate_unit": "m", "sign_convention": sign_convention,
            "simulated": simulated,
            "confirmed_start_xy_m": confirmed_start,
        })
        with (self.path / "points.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerow(self.columns)
            stream.flush()
            os.fsync(stream.fileno())
        self._status("running", False, True)
        self.log("运行开始")

    def append(self, row: PointRecord):
        with (self.path / "points.csv").open("a", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerow((
                row.index, *row.target, *(row.before_af or (None, None)),
                *(row.after_af or (None, None)), row.z, row.status, row.failure_stage,
            ))
            stream.flush()
            os.fsync(stream.fileno())

    def log(self, stage, index=0):
        with (self.path / "run.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{utc_now()} [{index}] {stage}\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _status(self, state, valid, recovery_required, index=0, stage=""):
        write_json(self.path / "status.json", {
            "started_at": self.started, "updated_at": utc_now(), "state": state,
            "result_valid": valid, "recovery_required": recovery_required,
            "point_index": index, "failure_stage": stage,
        })

    def finish(self, state, *, recovery_required, index=0, stage="", result=None):
        valid = state == "complete" and result is not None and not recovery_required
        if valid:
            write_json(self.path / "result.json", result)
        self.log(f"运行结束 [{state}] {stage}", index)
        self._status(state, valid, recovery_required, index, stage)


def pending_recovery(root: Path, *, simulated: bool):
    """Return run directories that require independent device confirmation."""
    directory = root / ("demo" if simulated else "field")
    if not directory.exists():
        return ()
    pending = []
    for run in directory.iterdir():
        if not run.is_dir():
            continue
        try:
            status = json.loads((run / "status.json").read_text(encoding="utf-8"))
            required = status["recovery_required"] is not False
            interrupted = status["state"] not in {"complete", "stopped", "failed"}
        except (OSError, ValueError, TypeError, KeyError):
            required, interrupted = True, False
        if not (required or interrupted):
            continue
        try:
            recovery = json.loads((run / "recovery.json").read_text(encoding="utf-8"))
            if (
                recovery["source"] == "operator_external_check"
                and recovery["xy_confirmed"] is True
                and recovery["af_z_confirmed"] is True
                and recovery["confirmed_at"]
            ):
                continue
        except (OSError, ValueError, TypeError, KeyError):
            pass
        pending.append(run)
    return tuple(sorted(pending))


def confirm_recovery(run: Path, *, xy_confirmed: bool, af_z_confirmed: bool):
    """Persist operator attestation after external checks on both subsystems."""
    if xy_confirmed is not True or af_z_confirmed is not True:
        raise ValueError("须分别确认 XY 和 AF/Z 已结束")
    write_json(run / "recovery.json", {
        "confirmed_at": utc_now(), "source": "operator_external_check",
        "xy_confirmed": True, "af_z_confirmed": True,
    })
