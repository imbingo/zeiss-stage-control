"""Single-XY upper/lower mark capture with shared recovery records."""

from dataclasses import asdict, dataclass
from math import isfinite, hypot
from pathlib import Path
import os
from time import monotonic
from uuid import uuid4

from zen_hardware import Motion
from zen_images import write_tiff
from zen_records import pending_recovery, utc_now, write_json


class CaptureStopped(RuntimeError):
    pass


@dataclass(frozen=True)
class MarkLayer:
    experiment: str
    z_m: float
    half_range_m: float

    @property
    def bounds(self):
        return self.z_m - self.half_range_m, self.z_m + self.half_range_m

    def validate(self):
        if not isinstance(self.experiment, str) or not self.experiment.strip():
            raise ValueError("请填写对应层的 ZEN 拍摄实验名")
        if not all(map(isfinite, (self.z_m, self.half_range_m, *self.bounds))) or self.half_range_m <= 0:
            raise ValueError("示教 Z 须为有限数字，AF 半范围须为有限正数")


@dataclass(frozen=True)
class MarkRecipe:
    upper: MarkLayer | None
    lower: MarkLayer | None
    xy_tolerance_m: float = 1e-6
    z_tolerance_m: float = .5e-6
    af_timeout_s: float = 10
    arrival_timeout_s: float = 5
    capture_timeout_s: float = 20
    settle_s: float = .2
    xy_mode: str = "motorized"
    capture_kind: str = "camera_snap"
    use_autofocus: bool = True

    def validate(self, layers=("upper", "lower")):
        if self.xy_mode not in ("motorized", "no_xy") or self.capture_kind not in ("camera_snap", "confocal"):
            raise ValueError("XY 模式或采集方式无效")
        if tuple(layers) not in (("upper",), ("lower",), ("upper", "lower")):
            raise ValueError("采集层选择无效")
        values = (self.xy_tolerance_m, self.z_tolerance_m, self.af_timeout_s,
                  self.arrival_timeout_s, self.capture_timeout_s, self.settle_s)
        if not all(isfinite(v) and v > 0 for v in values):
            raise ValueError("容差、超时和稳定等待时间须为有限正数")
        for key in layers:
            layer = getattr(self, key)
            if layer is None:
                raise ValueError("请先配置并示教所选层的 Z")
            layer.validate()
        if type(self.use_autofocus) is not bool:
            raise ValueError("自动对焦选项无效")
        if len(layers) == 2 and self.use_autofocus:
            a, b = self.upper.bounds, self.lower.bounds
            if not (a[1] < b[0] or b[1] < a[0]):
                raise ValueError("上下层 AF 搜索范围不能重叠，请减小半范围或重新示教 Z")

    def to_dict(self):
        return {"schema_version": 1, "kind": "mark_capture", "coordinate_unit": "m", **asdict(self)}

    @classmethod
    def from_dict(cls, value):
        if value.get("schema_version") != 1 or value.get("kind") != "mark_capture" or value.get("coordinate_unit") != "m":
            raise ValueError("不支持的采图配置版本或单位")
        keys = ("xy_tolerance_m", "z_tolerance_m", "af_timeout_s", "arrival_timeout_s", "capture_timeout_s", "settle_s")
        recipe = cls(*(MarkLayer(**value[key]) if value[key] is not None else None for key in ("upper", "lower")),
                     **{key: value[key] for key in keys},
                     xy_mode=value.get("xy_mode", "motorized"), capture_kind=value.get("capture_kind", "camera_snap"),
                     use_autofocus=value.get("use_autofocus", True))
        recipe.validate()
        return recipe


class MarkRecords:
    def __init__(self, root, output, recipe, layers, simulated):
        if pending_recovery(root, simulated=simulated):
            raise RuntimeError("存在未确认的设备状态")
        identity = uuid4().hex
        self.path = root / ("demo" if simulated else "field") / identity
        self.path.mkdir(parents=True)
        self.output = Path(output).expanduser().resolve() / f"{'demo' if simulated else 'mark'}_{identity}"
        self.started = utc_now()
        self.document = {"schema_version": 1, "kind": "mark_capture", "simulated": simulated,
                         "started_at": self.started, "run_id": identity,
                         "recipe": recipe.to_dict(), "requested_layers": layers, "images": {}}
        # Write a fail-closed status in the central scan root BEFORE any external
        # output operations. Restart/reconnect therefore sees incomplete captures.
        self.status("running", recovery=True, stage="准备输出目录")
        self.output.mkdir(parents=True)
        write_json(self.path / "recipe.json", self.document)
        self.sync()

    def status(self, state, *, recovery, stage):
        status = {"state": state, "result_valid": False, "recovery_required": recovery,
                  "started_at": self.started, "updated_at": utc_now(),
                  "kind": "mark_capture", "failure_stage": stage,
                  "pair_complete": state == "complete" and len(self.document["images"]) == 2,
                  "output_directory": str(self.output)}
        write_json(self.path / "status.json", status)
        self.document["status"] = status

    def sync(self):
        write_json(self.path / "capture.json", self.document)
        write_json(self.output / "capture.json", self.document)

    def progress(self, stage):
        self.status("running", recovery=True, stage=stage)
        with (self.path / "run.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{utc_now()} {stage}\n")
            stream.flush()
            os.fsync(stream.fileno())

    def save(self, key, frame, extra):
        info = {"layer": key, "captured_at": utc_now(), "simulated": self.document["simulated"], **extra}
        write_tiff(self.output / f"{key}.tif", frame, info)
        self.document["images"][key] = {"file": f"{key}.tif", **frame.metadata(), **info}
        self.sync()

    def finish(self, state, *, recovery, stage=""):
        self.status(state, recovery=recovery, stage=stage)
        self.sync()


def capture_marks(device, recipe, layers, stop, records, progress):
    """Never issue XY moves, implicit returns or repeated exposure commands."""
    recipe.validate(layers)

    def checkpoint():
        if stop.is_set():
            raise CaptureStopped("已停止后续拍摄，需确认 XY、Z、AF 和拍摄均已结束")

    def read_xy():
        xy = device.read_xy()
        if len(xy) != 2 or not all(map(isfinite, xy)) or device.motion_state() is not Motion.IDLE:
            raise ValueError("XY 位置或空闲状态无法确认")
        return xy

    checkpoint()
    xy_enabled = recipe.xy_mode == "motorized"
    start_xy = read_xy() if xy_enabled else None
    optics = device.read_optics()
    records.document["optics_positions"] = optics
    records.document["optics_verified"] = [v is not None for v in optics]
    records.document["xy_verified"] = xy_enabled
    records.document["xy_mode"] = recipe.xy_mode
    records.document["start_xy_m"] = start_xy
    records.sync()

    def guard():
        checkpoint()
        xy = read_xy() if xy_enabled else None
        if device.read_optics() != optics:
            raise ValueError("物镜或中间倍率位置发生变化，已停止采图")
        if xy_enabled and hypot(xy[0] - start_xy[0], xy[1] - start_xy[1]) > recipe.xy_tolerance_m:
            raise ValueError("XY 已偏离起点，已停止采图")
        return xy

    def stage(text):
        records.progress(text)
        progress(text)
        guard()

    def settle_z(target):
        deadline, stable = monotonic() + recipe.arrival_timeout_s, 0
        while True:
            guard()
            z = device.read_z()
            if not isfinite(z):
                raise ValueError("无效的 Z 回读")
            stable = stable + 1 if abs(z-target) <= recipe.z_tolerance_m else 0
            if stable >= 3:
                break
            if monotonic() >= deadline:
                raise TimeoutError("Z 到位确认超时")
            if stop.wait(.05):
                checkpoint()
        if stop.wait(recipe.settle_s):
            checkpoint()
        guard()
        z = device.read_z()
        if not isfinite(z) or abs(z-target) > recipe.z_tolerance_m:
            raise ValueError("Z 在稳定等待期间发生变化")
        return z

    first = None
    for key in layers:
        layer = getattr(recipe, key)
        title = "上 mark" if key == "upper" else "下 mark"
        stage(f"{title} · 加载临时实验和限定 AF 范围")
        actual_settings = device.prepare_mark_experiment(layer.experiment, *layer.bounds, use_autofocus=recipe.use_autofocus)
        guard()
        stage(f"{title} · Z 定位")
        device.move_z(layer.z_m)
        actual_z = settle_z(layer.z_m)
        focused_z = actual_z
        if recipe.use_autofocus:
            stage(f"{title} · 局部 AF")
            focused_z = device.autofocus(recipe.af_timeout_s)
        low, high = layer.bounds
        if not isfinite(focused_z) or not low <= focused_z <= high:
            raise ValueError("AF 返回焦面超出所选层的限定范围")
        actual_z = settle_z(focused_z)
        if not low <= actual_z <= high:
            raise ValueError("AF 后 Z 回读超出限定范围")
        stage(f"{title} · 单张拍摄")
        frame = device.capture_frame(recipe.capture_timeout_s, stop, f"mark_{records.document['run_id']}_{key}", kind=recipe.capture_kind)
        frame.array()  # Validate before publishing a file or proceeding to the other layer.
        guard()
        after_z = device.read_z()
        if (not isfinite(after_z) or not low <= after_z <= high
                or abs(after_z-actual_z) > recipe.z_tolerance_m):
            raise ValueError("拍摄过程中 Z 改变或偏离焦面")
        x, y, z = frame.xyz_m
        if frame.xy_verified != xy_enabled:
            raise ValueError("图像 XY 校验标记与连接模式不一致")
        if (xy_enabled and hypot(x-start_xy[0], y-start_xy[1]) > recipe.xy_tolerance_m
                or not low <= z <= high or abs(z-actual_z) > recipe.z_tolerance_m):
            raise ValueError("图像记录的 XYZ 与已确认拍摄位置不符")
        if first is not None and not frame.matches_geometry(first):
            raise ValueError("上下图像的尺寸、ROI、像素类型或像素标定不同，未保存下图")
        first = frame
        stage(f"{title} · 保存 TIFF")
        records.save(key, frame, {"xy_m": start_xy, "af_z_m": focused_z if recipe.use_autofocus else None,
                                 "readback_z_m": after_z, "experiment": layer.experiment,
                                 "af_settings": actual_settings})
        checkpoint()
