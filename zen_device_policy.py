"""Product policy: no manual recovery questionnaire; retain live state checks."""

import json
from pathlib import Path
from zen_records import write_json


def manual_recovery_enabled(root):
    try:
        return (
            json.loads(
                (Path(root) / "device_policy.json").read_text(encoding="utf-8")
            ).get("manual_recovery", True)
            is not False
        )
    except (OSError, ValueError, TypeError):
        return True


def use_live_checks(root):
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    write_json(
        path / "device_policy.json",
        {
            "schema_version": 1,
            "manual_recovery": False,
            "mode": "live_device_checks",
            "note": "No automatic attestation or deletion of historical fault evidence.",
        },
    )
