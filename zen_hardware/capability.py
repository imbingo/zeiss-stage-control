"""Per-method capability matrix for the connected ZEN/Gateway.

ZEN API surfaces differ by ZEN version (e.g. 3.12 lacks several v2 services
present in the 2026.11.1 API) and by installed hardware. A method that once
returned UNIMPLEMENTED within a session is unlikely to start working seconds
later: record it, fail fast without re-issuing the RPC, and persist the matrix
so the next session starts informed. Persisted entries are revalidated once
per session so a ZEN upgrade is picked up automatically.
"""

import json
from pathlib import Path

from zen_records import utc_now, write_json


OK = "ok"
UNIMPLEMENTED = "unimplemented"


class CapabilityMatrix:
    """Record which Gateway methods are usable on this ZEN version/hardware."""

    def __init__(self):
        self.methods = {}          # method -> {"status", "seen_at", "session"}
        self.services = None       # last reflection result: tuple of names, or None
        self.missing_services = () # registered-service diff, set by note_services()

    def record(self, method, code):
        """Feed an RPC outcome. Only OK/UNIMPLEMENTED carry capability meaning."""
        status = {"OK": OK, "UNIMPLEMENTED": UNIMPLEMENTED}.get(code)
        if status is None:
            return
        self.methods[method] = {"status": status, "seen_at": utc_now(), "session": True}

    def note_services(self, names, candidate_services):
        """Remember the reflection listing and flag absent candidate services."""
        self.services = tuple(sorted(names))
        registered = set(names)
        self.missing_services = tuple(
            sorted(service for service in candidate_services if service not in registered)
        )

    def unsupported(self, method):
        """True when this method should not be re-issued this session."""
        entry = self.methods.get(method)
        if entry and entry.get("status") == UNIMPLEMENTED and entry.get("session"):
            return True
        service = method.rpartition(".")[0]
        return bool(service) and service in self.missing_services

    def unimplemented(self):
        """Methods confirmed or persisted as unavailable on this system."""
        return sorted(
            method for method, entry in self.methods.items()
            if entry.get("status") == UNIMPLEMENTED
        )

    def to_dict(self):
        return {
            "schema_version": 1, "kind": "capability_report", "recorded_at": utc_now(),
            "registered_services": list(self.services) if self.services else None,
            "missing_candidate_services": list(self.missing_services),
            "methods": {
                method: {"status": entry["status"], "seen_at": entry["seen_at"]}
                for method, entry in sorted(self.methods.items())
            },
        }

    def save(self, path):
        write_json(Path(path), self.to_dict())

    @classmethod
    def load(cls, path):
        matrix = cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return matrix
        if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("kind") != "capability_report":
            return matrix
        methods = data.get("methods", {})
        if isinstance(methods, dict):
            for method, entry in methods.items():
                if isinstance(method, str) and isinstance(entry, dict) and entry.get("status") in (OK, UNIMPLEMENTED):
                    # Persisted entries allow one revalidation call per session.
                    matrix.methods[method] = {
                        "status": entry["status"],
                        "seen_at": str(entry.get("seen_at", "")),
                        "session": False,
                    }
        return matrix
