"""Persist a run incrementally using an explicit, credential-free schema."""

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from uuid import uuid4

from zen_acquisition import PointRecord


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def software_version():
    """Report the installed build, or clearly say this is a source checkout.

    A hard-coded version string makes every historical run claim to belong to
    whichever release happened to be current when the code was written.
    Packaged builds can inject ZEN_APP_VERSION.
    """
    override = os.environ.get("ZEN_APP_VERSION", "").strip()
    if override:
        return override
    try:
        return metadata.version("zeiss-stage-control")
    except metadata.PackageNotFoundError:
        return "2.7.0+source"


def canonical_sha256(value):
    """Stable hash of a canonicalised dict; metadata is excluded by the caller."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_manifest(root, *, run_id, files, base=None):
    """Freeze a content-addressed list of every artifact a run produced.

    The image directory lives outside the central record directory, so a path
    string alone cannot prove a TIFF still exists or was not replaced.
    """
    root = Path(root)
    base = Path(base) if base is not None else None
    entries = []
    for item in sorted({Path(f) for f in files}):
        if not item.is_file():
            continue
        digest = hashlib.sha256()
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        rel = str(item.relative_to(base)) if base and item.is_relative_to(base) else item.name
        entries.append({"rel_path": rel, "sha256": digest.hexdigest(),
                        "bytes": item.stat().st_size})
    document = {"kind": "artifact_manifest", "schema_version": 1, "run_id": run_id,
                "created_at": utc_now(), "artifact_count": len(entries),
                "artifacts": entries,
                "manifest_sha256": canonical_sha256({"artifacts": entries})}
    write_json(root / "manifest.json", document)
    return document


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
            "schema_version": 1, "software_version": software_version(),
            "model_version": "1.1",
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


RECOVERY_ITEMS = (
    ("xy_idle", "载物台 XY 动作已结束、样品已固定"),
    ("af_z_idle", "软件 AF、Z 轴与拍摄均已结束"),
    ("experiment_idle", "ZEN 实验与采集已停止，无后台任务"),
    ("optics_unchanged", "物镜、倍率与光路未被改变"),
)


def confirm_recovery(run: Path, *, xy_confirmed=None, af_z_confirmed=None,
                     operator="", items=None, reason="", readbacks=None):
    """Append one signed, per-run, per-item operator attestation.

    The legacy two-boolean call still works and becomes a two-item checklist.
    Every item must be explicitly confirmed; callers are responsible for
    writing one record per run instead of silently unlocking a whole batch.
    """
    run = Path(run)
    checklist = []
    for item in (items or ()):
        key = str(item.get("key", ""))
        checklist.append({"key": key, "label": str(item.get("label", key))[:200],
                          "confirmed": item.get("confirmed") is True,
                          "method": str(item.get("method", "operator_external_check"))[:120],
                          "value": str(item.get("value", ""))[:200]})
    if not checklist:
        checklist = [
            {"key": "xy_idle", "label": RECOVERY_ITEMS[0][1],
             "confirmed": xy_confirmed is True,
             "method": "operator_external_check", "value": ""},
            {"key": "af_z_idle", "label": RECOVERY_ITEMS[1][1],
             "confirmed": af_z_confirmed is True,
             "method": "operator_external_check", "value": ""},
        ]
    by_key = {item["key"]: item for item in checklist}
    xy = by_key.get("xy_idle", {}).get("confirmed", xy_confirmed is True)
    af_z = by_key.get("af_z_idle", {}).get("confirmed", af_z_confirmed is True)
    if xy is not True or af_z is not True:
        raise ValueError("须分别确认 XY 和 AF/Z 已结束")
    if not all(item["confirmed"] is True for item in checklist):
        raise ValueError("恢复确认的每一项都必须逐项勾选")
    try:
        json.dumps(readbacks or {}, allow_nan=False)
        safe_readbacks = readbacks or {}
    except (TypeError, ValueError):
        safe_readbacks = {"note": "设备回读值未能序列化，未写入"}
    record = {
        "source": "operator_external_check", "confirmed_at": utc_now(),
        "xy_confirmed": True, "af_z_confirmed": True,
        "run_id": run.name, "operator": str(operator or "")[:120],
        "reason": str(reason or "")[:500], "items": checklist,
        "readbacks": safe_readbacks,
    }
    record["attestation_sha256"] = canonical_sha256(
        {k: v for k, v in record.items() if k != "attestation_sha256"})
    write_json(run / "recovery.json", record)
    with (run / "recovery.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return record
