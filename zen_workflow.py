"""Background task ownership and result publication for the workbench."""

from dataclasses import dataclass
from math import isfinite
from queue import SimpleQueue
from threading import Event, Lock, Thread

from zen_acquisition import AcquisitionFailed, acquire
from zen_hardware import AutofocusTimeout, DeviceDisconnected, Motion
from zen_hardware.gateway import connection_hint
from zen_records import RunRecords, pending_recovery


@dataclass(frozen=True)
class Recipe:
    name: str
    experiment: str
    targets: tuple
    tolerance_m: float
    af_timeout: float
    arrival_timeout: float
    sign_convention: str


class Workflow:
    def __init__(self, device, root, *, simulated, validate, fit):
        self.device = device
        self.root = root
        self.simulated = simulated
        self.validate = validate
        self.fit = fit
        self.events = SimpleQueue()
        self._gate = Lock()
        self._stop = Event()
        self.result = None
        self.result_path = None
        self.state = "未连接"
        self.mark_run_path = None
        self.mark_output_path = None

    @property
    def busy(self):
        return self._gate.locked()

    def _launch(self, action):
        if not self._gate.acquire(blocking=False):
            raise RuntimeError("已有设备任务")
        self._stop.clear()

        def worker():
            try:
                action()
            finally:
                self._gate.release()

        try:
            Thread(target=worker, daemon=False).start()
        except BaseException:
            self._gate.release()
            raise

    def _state(self, state, index=0, detail=""):
        self.state = state
        self.events.put(("state", state, index, detail))

    def connect(self):
        def action():
            self.result = None
            self.result_path = None
            self._state("连接检查")
            try:
                self.device.connect()
                xy = self.device.read_xy()
                if not all(map(isfinite, xy)):
                    raise ValueError("无效 XY")
                self.events.put(("xy", xy))
                motion = self.device.motion_state()
                recovery = pending_recovery(self.root, simulated=self.simulated)
            except Exception as exc:
                detail = "模拟连接检查失败" if self.simulated else connection_hint(exc)
                self._state("未连接", detail=detail)
            else:
                if recovery:
                    self._state("设备状态待确认")
                elif motion is not Motion.IDLE:
                    self._state("设备状态待确认", detail="XY 未空闲，请在设备端检查后重新连接")
                else:
                    self._state("就绪")

        self._launch(action)

    def start(self, recipe, *, path_confirmed, single_point=False, confirmed_start=None):
        if not path_confirmed:
            raise ValueError("请确认运动顺序及物镜间隙")
        if not self.simulated and not single_point and confirmed_start is None:
            raise ValueError("现场批量须确认路径起点")
        if single_point:
            if not recipe.name.strip() or not recipe.experiment.strip():
                raise ValueError("配方与实验名称不能为空")
            if not all(isfinite(value) and value > 0 for value in (
                recipe.tolerance_m, recipe.af_timeout, recipe.arrival_timeout,
            )):
                raise ValueError("容差和超时必须为有限正数")
        else:
            self.validate(recipe)

        def action():
            self.result = None
            self.result_path = None
            records = None
            rows = []
            try:
                targets = recipe.targets
                if single_point:
                    targets = (self.device.read_xy(),)
                records = RunRecords(
                    self.root, name=recipe.name, experiment=recipe.experiment,
                    targets=targets, tolerance_m=recipe.tolerance_m,
                    af_timeout=recipe.af_timeout, arrival_timeout=recipe.arrival_timeout,
                    sign_convention=recipe.sign_convention, simulated=self.simulated,
                    confirmed_start=confirmed_start,
                )

                def persist(row):
                    records.append(row)
                    rows.append(row)
                    self.events.put(("row", row))

                def progress(stage, index):
                    records.log(stage, index)
                    if not self._stop.is_set():
                        self._state(stage, index)

                acquire(
                    self.device, targets, recipe.experiment, recipe.tolerance_m,
                    recipe.af_timeout, recipe.arrival_timeout, self._stop, persist, progress,
                    move=not single_point,
                    confirmed_start=confirmed_start,
                )
                result = None if single_point else self.fit(tuple(rows))
                if self._stop.is_set():
                    raise AcquisitionFailed("结果发布", len(rows), stopped=True,
                                            recovery_required=False)
                records.finish("complete", recovery_required=False, result=result)
                self.result = result
                if single_point:
                    self.events.put(("verified_xy", rows[0].after_af))
                    self._state("就绪", 1, "单点 AF 验证完成")
                else:
                    self.result_path = records.path
                    self.events.put(("result", result, records.path))
                    self._state("完成")
            except AcquisitionFailed as exc:
                if isinstance(exc.__cause__, AutofocusTimeout):
                    reason = "AF 超时"
                elif isinstance(exc.__cause__, DeviceDisconnected):
                    reason = "Gateway 连接中断"
                elif exc.stopped:
                    reason = "已停止后续采集"
                else:
                    reason = "采集失败"
                try:
                    records.log(reason, exc.index)
                    records.finish("stopped" if exc.stopped else "failed",
                                   recovery_required=exc.recovery_required,
                                   index=exc.index, stage=exc.stage)
                except OSError:
                    self._state("设备状态待确认", exc.index, "写入运行状态失败")
                else:
                    state = "设备状态待确认" if exc.recovery_required else "已中止"
                    self._state(state, exc.index, f"{exc.stage} · {reason} · 本轮无有效角度")
            except Exception:
                self._state("设备状态待确认", detail="运行记录或计算失败")

        if self.state not in {"就绪", "完成", "已中止"}:
            raise RuntimeError("当前状态禁止启动")
        self._launch(action)

    def stop(self):
        if self.busy:
            self._stop.set()
            self.events.put(("stop", "已请求停止后续采集"))

    def teach_xy(self):
        if self.state not in {"就绪", "完成", "已中止"}:
            raise RuntimeError("当前状态禁止示教")

        def action():
            try:
                if pending_recovery(self.root, simulated=self.simulated):
                    raise RuntimeError("设备状态待确认")
                if self.device.motion_state() is not Motion.IDLE:
                    raise RuntimeError("XY 未空闲")
                xy = self.device.read_xy()
                if not all(map(isfinite, xy)):
                    raise ValueError("无效 XY")
            except Exception:
                self._state("设备状态待确认", detail="示教读取失败，请检查设备后重新连接")
            else:
                self.events.put(("taught", xy))

        self._launch(action)

    def invalidate(self):
        if self.busy:
            raise RuntimeError("运行期间锁定配方与点位")
        self.result = None
        self.result_path = None

    def read_mark_z(self, layer):
        if layer not in ("upper", "lower"):
            raise ValueError("未知的 mark 层")
        if self.state not in {"就绪", "完成", "已中止"}:
            raise RuntimeError("请先连接并确认设备就绪")
        def action():
            try:
                if pending_recovery(self.root, simulated=self.simulated):
                    raise RuntimeError("设备状态待确认")
                z = self.device.read_z()
                xy = self.device.read_xy()
                if not isfinite(z) or not all(map(isfinite, xy)):
                    raise ValueError("无效的 XYZ 回读")
                self.events.put(("mark_z", layer, z, xy))
            except Exception as error:
                self.events.put(("mark_error", self._mark_hint(error)))
        self._launch(action)

    @staticmethod
    def _mark_hint(error):
        from zen_hardware.mark_gateway import MarkCapabilityError
        if isinstance(error, (MarkCapabilityError, ValueError, TimeoutError, InterruptedError)):
            return str(error)
        if isinstance(error, ImportError):
            return "缺少采图依赖或存根，请使用完整新版安装包"
        if isinstance(error, OSError) and not isinstance(error, DeviceDisconnected):
            return "图像或运行记录读写失败，请检查目录权限和剩余空间"
        try:
            import grpc
            if isinstance(error, grpc.RpcError) and error.code() == grpc.StatusCode.UNIMPLEMENTED:
                return "现场缺少采图所需的 Focus v2、物镜／中间倍率状态或图像流接口"
        except ImportError:
            pass
        return "采图接口调用失败，请检查设备和 ZEN 实验；详细位置见运行阶段"

    def start_mark_capture(self, recipe, output, *, layers=("upper", "lower"), confirmed=False):
        from pathlib import Path
        from zen_marks import MarkRecords, capture_marks, CaptureStopped
        if not confirmed:
            raise ValueError("请先确认两层 Z 路径、搜索范围、物镜间隙和拍摄参数")
        recipe.validate(layers)
        if self.state not in {"就绪", "完成", "已中止"}:
            raise RuntimeError("请先连接并确认设备就绪")
        if pending_recovery(self.root, simulated=self.simulated):
            raise RuntimeError("存在未确认的设备状态，请先完成恢复确认")
        if Path(output).expanduser().resolve().is_relative_to(self.root.resolve()):
            raise ValueError("图像输出请选择运行记录目录之外的位置")
        previous = self.state
        self.mark_run_path = None
        self.mark_output_path = None
        def action():
            records, starting = None, False
            try:
                # Capability checks are read-only. Missing optional services do
                # not disable the existing angle-scanning capability.
                self.device.read_z()
                self.device.read_optics()
                starting = True
                records = MarkRecords(self.root, output, recipe, layers, self.simulated)
                self.mark_run_path, self.mark_output_path = records.path, records.output
                self.result = None
                self.result_path = None
                capture_marks(self.device, recipe, layers, self._stop, records,
                              lambda detail: self._state("对位采图", detail=detail))
                records.finish("complete", recovery=False)
                final = "下 mark" if layers[-1] == "lower" else "上 mark"
                self._state("就绪", detail=f"采图完成，停留在{final}焦面；图片目录：{records.output}")
            except Exception as error:
                stopped = isinstance(error, (CaptureStopped, InterruptedError)) or self._stop.is_set()
                detail = "已停止后续拍摄" if stopped else self._mark_hint(error)
                if records is not None:
                    try:
                        records.finish("stopped" if stopped else "failed", recovery=True,
                                       stage=records.document["status"]["failure_stage"] + " · " + detail)
                    except OSError:
                        detail = "图像或状态写入失败，请检查运行记录与输出目录"
                if starting:
                    self._state("设备状态待确认", detail=detail + "；请确认 XY、Z、AF 与拍摄均已结束")
                else:
                    self._state(previous, detail=detail)
                self.events.put(("mark_error", detail))
        self._launch(action)
