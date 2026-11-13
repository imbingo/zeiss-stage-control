"""Automatic UI preferences, excluding credentials and action approvals."""

import json
from pathlib import Path
from zen_records import write_json


COMMON = ('xy_tolerance_m', 'z_tolerance_m', 'af_timeout_s', 'arrival_timeout_s', 'capture_timeout_s', 'settle_s')


def text(value):
    return value if isinstance(value, str) and len(value) <= 4096 else None


def sanitize(value):
    result = {'ui_schema_version': 1}
    if value.get('xy_mode') in ('motorized', 'no_xy'):
        result['xy_mode'] = value['xy_mode']
    connection = value.get('connection', {})
    if isinstance(connection, dict):
        result['connection'] = {key: val for key in ('ZEN_GATEWAY_ADDRESS', 'ZEN_GATEWAY_CA', 'ZEN_ALLOW_FOCUS_V1', 'ZEN_ALLOW_UNVERIFIED_AF', 'ZEN_ALLOW_DEFINITE_FOCUS')
                                if (val := text(connection.get(key))) is not None}
    capture = value.get('mark_capture', {})
    if "mark_capture" in value and isinstance(capture, dict):
        saved = {}
        for key in ('output', 'reported_zen_version'):
            if (val := text(capture.get(key))) is not None:saved[key] = val
        if capture.get('capture_kind') in ('camera_snap', 'confocal'):
            saved['capture_kind'] = capture['capture_kind']
        for key in ('use_autofocus', 'simulated_configuration'):
            if type(capture.get(key)) is bool:saved[key] = capture[key]
        for group, keys in (('upper', ('experiment', 'z_m', 'half_range_m')),
                            ('lower', ('experiment', 'z_m', 'half_range_m')), ('common', COMMON)):
            source = capture.get(group, {})
            if isinstance(source, dict):
                saved[group] = {key: val for key in keys if (val := text(source.get(key))) is not None}
        result['mark_capture'] = saved
    hardware = value.get("hardware_test", {})
    if isinstance(hardware, dict):
        allowed = ("experiment_name", "camera_id", "z_um", "x_um", "y_um", "low_um", "high_um", "first_um", "last_um", "interval_um", "af_timeout_s", "exposure_value", "output_name", "rpc_timeout_s")
        result["hardware_test"] = {k: v for k in allowed if (v := text(hardware.get(k))) is not None}
    return result


class Preferences:
    def __init__(self, path):
        self.path = Path(path)
        self.warning = ''
        self.data = sanitize({})
        try:
            if self.path.exists():
                if self.path.stat().st_size > 1024*1024:
                    raise ValueError('oversized')
                source = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(source, dict) or source.get('ui_schema_version') != 1:
                    raise ValueError('unsupported')
                self.data = sanitize(source)
        except (OSError, ValueError, TypeError):
            self.warning = '上次自动配置不可读，已保留原文件且停用自动覆盖；本次可手动填写或加载配置。'

    def update(self, **sections):
        if self.warning:
            return False
        candidate = sanitize({**self.data, **sections})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self.path, candidate)
        except (OSError, ValueError):
            return False
        self.data = candidate
        return True
