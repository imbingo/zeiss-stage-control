"""Automatic UI preferences, excluding credentials and action approvals."""

import json
from math import isfinite
from pathlib import Path
from zen_records import write_json


COMMON = ('xy_tolerance_m', 'z_tolerance_m', 'af_timeout_s', 'arrival_timeout_s', 'capture_timeout_s', 'settle_s')


def text(value):
    return value if isinstance(value, str) and len(value) <= 4096 else None


def entry(value):
    """Keep legacy raw strings; accept finite Qt numbers as the same schema."""
    if isinstance(value, str):
        return text(value)
    if type(value) in (int, float) and isfinite(value):
        return str(value)
    return None


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
        # Legacy Tk keys end in _m but contain raw UI values in MICROMETRES.
        # Keep that wire-compatible entry schema and mark the unit explicitly.
        unit = capture.get('entry_unit', 'um')
        if unit not in ('um', 'm'):
            raise ValueError('unsupported preference entry unit')
        saved = {'entry_unit': 'um'}
        for key in ('output', 'reported_zen_version'):
            if (val := text(capture.get(key, capture.get('output_dir') if key == 'output' else None))) is not None:saved[key] = val
        if capture.get('capture_kind') in ('camera_snap', 'confocal'):
            saved['capture_kind'] = capture['capture_kind']
        for key in ('use_autofocus', 'simulated_configuration'):
            if type(capture.get(key)) is bool:saved[key] = capture[key]
        for group, keys in (('upper', ('experiment', 'z_m', 'half_range_m')),
                            ('lower', ('experiment', 'z_m', 'half_range_m')), ('common', COMMON)):
            source = capture.get(group, {})
            if isinstance(source, dict):
                source = dict(source)
                if group == 'common':
                    for unit_key in ('xy_tolerance', 'z_tolerance'):
                        if unit_key+'_um' in source:
                            source[unit_key+'_m'] = source[unit_key+'_um']
                            # Already in the canonical UI entry unit.
                            source.pop(unit_key+'_um')
                        elif unit == 'm' and unit_key+'_m' in source:
                            try:source[unit_key+'_m'] = float(source[unit_key+'_m']) * 1e6
                            except (TypeError, ValueError, OverflowError):pass
                elif unit == 'm':
                    for key in ('z_m', 'half_range_m'):
                        if key in source:
                            try:source[key] = float(source[key]) * 1e6
                            except (TypeError, ValueError, OverflowError):pass
                saved[group] = {key: val for key in keys if (val := entry(source.get(key))) is not None}
        result['mark_capture'] = saved
    hardware = value.get("hardware_test", {})
    if isinstance(hardware, dict):
        allowed = ("experiment_name", "camera_id", "z_um", "x_um", "y_um", "low_um", "high_um", "first_um", "last_um", "interval_um", "af_timeout_s", "exposure_value", "output_name", "rpc_timeout_s")
        result["hardware_test"] = {k: v for k in allowed if (v := text(hardware.get(k))) is not None}
    sequence = value.get('sequence')
    if isinstance(sequence, dict):
        if sequence.get('entry_unit', 'um') != 'um':
            raise ValueError('unsupported sequence preference unit')
        keys=('name','safe_z_um','tilt_z_um','z_min_um','z_max_um','max_angle_urad','max_rms_um',
              'upper_experiment','lower_experiment','upper_half_um','lower_half_um',
              'z_tolerance_um','settle_s','capture_timeout_s','output')
        fields=sequence.get('fields',{})
        rows=sequence.get('rows',[])
        if not isinstance(fields,dict) or not isinstance(rows,list) or len(rows)>1000:
            raise ValueError('invalid sequence preferences')
        saved={'entry_unit':'um','fields':{k:v for k in keys if (v:=entry(fields.get(k))) is not None},'rows':[]}
        for row in rows:
            if not isinstance(row,list) or len(row)!=6 or any(text(v) is None for v in row):
                raise ValueError('invalid sequence station row')
            saved['rows'].append(list(row))
        for key in ('use_autofocus','simulated_configuration'):
            if type(sequence.get(key)) is bool:saved[key]=sequence[key]
        if sequence.get('capture_kind') in ('confocal','camera_snap'):
            saved['capture_kind']=sequence['capture_kind']
        tilt=sequence.get('tilt')
        if tilt is not None:
            keys=('name','experiment','targets','tolerance_m','af_timeout','arrival_timeout','sign_convention')
            if sequence.get('tilt_coordinate_unit')!='m' or not isinstance(tilt,dict) or set(tilt)!=set(keys):
                raise ValueError('invalid sequence tilt schema/unit')
            if any(text(tilt.get(k)) is None for k in ('name','experiment','sign_convention')):
                raise ValueError('invalid sequence tilt strings')
            if any(type(tilt.get(k)) not in (int,float) or not isfinite(tilt[k]) for k in ('tolerance_m','af_timeout','arrival_timeout')):
                raise ValueError('invalid sequence tilt parameters')
            targets=tilt['targets']
            if not isinstance(targets,(list,tuple)) or len(targets)>1000 or any(
                not isinstance(p,(list,tuple)) or len(p)!=2 or any(type(v) not in (int,float) or not isfinite(v) for v in p) for p in targets):
                raise ValueError('invalid sequence tilt points')
            saved['tilt_coordinate_unit']='m';saved['tilt']={k:tilt[k] for k in keys}
        result['sequence']=saved
    session = value.get('session', {})
    if isinstance(session, dict):
        result['session'] = {k: v for k in ('sample', 'operator') if (v := text(session.get(k))) is not None}
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
        try:
            candidate = sanitize({**self.data, **sections})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self.path, candidate)
        except (OSError, ValueError):
            return False
        self.data = candidate
        return True
