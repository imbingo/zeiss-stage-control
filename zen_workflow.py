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
        self.mark_diagnostic_report = None

    @property
    def xy_enabled(self):
        return getattr(self.device, "xy_enabled", True)

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
                if self.xy_enabled:
                    xy = self.device.read_xy()
                    if not all(map(isfinite, xy)):
                        raise ValueError("无效 XY")
                    self.events.put(("xy", xy))
                    motion = self.device.motion_state()
                else:
                    z = self.device.read_z()
                    if not isfinite(z):
                        raise ValueError("无效 Z")
                    self.events.put(("z_only", z))
                    motion = None
                recovery = pending_recovery(self.root, simulated=self.simulated)
            except Exception as exc:
                detail = "模拟连接检查失败" if self.simulated else (connection_hint(exc) if self.xy_enabled else self._mark_hint(exc))
                self._state("未连接", detail=detail)
            else:
                if recovery:
                    self._state("设备状态待确认")
                elif self.xy_enabled and motion is not Motion.IDLE:
                    self._state("设备状态待确认", detail="XY 未空闲，请在设备端检查后重新连接")
                else:
                    self._state("就绪")

        self._launch(action)

    def start(self, recipe, *, path_confirmed, single_point=False, confirmed_start=None):
        if not self.xy_enabled:
            raise RuntimeError("无 XY 模式仅支持单点采图，不支持角度扫描或角度单点 AF")
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
        if not self.xy_enabled:
            raise RuntimeError("无 XY 模式不能示教 XY")
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
                xy = self.device.read_xy() if self.xy_enabled else None
                if not isfinite(z) or (xy is not None and not all(map(isfinite, xy))):
                    raise ValueError("无效的 XYZ 回读")
                self.events.put(("mark_z", layer, z, xy))
            except Exception as error:
                self.events.put(("mark_error", self._mark_hint(error)))
        self._launch(action)

    @staticmethod
    def _mark_hint(error):
        from zen_hardware.rpc_diagnostics import diagnostic_from_error
        diagnostic = diagnostic_from_error(error)
        if diagnostic:
            return diagnostic["message"]
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
                return "现场未提供所需的 Z／实验采集／图像流接口；不会自动改用相机拍照，请核对 ZEN 版本及实验"
        except ImportError:
            pass
        return "采图接口调用失败，请检查设备和 ZEN 实验；详细位置见运行阶段"

    def start_mark_capture(self, recipe, output, *, layers=("upper", "lower"), confirmed=False, experiment_loading_confirmed=False):
        from pathlib import Path
        from zen_marks import MarkRecords, capture_marks, CaptureStopped
        if not confirmed:
            raise ValueError("请先确认两层 Z 路径、搜索范围、物镜间隙和拍摄参数")
        if not experiment_loading_confirmed:
            raise ValueError("未确认 ZEN 实验加载/运行不会换物镜或恢复位置，禁止加载和拍摄；读到 Z 不代表转盘已锁定")
        recipe.validate(layers)
        if (recipe.xy_mode == "motorized") != self.xy_enabled:
            raise ValueError("采图配置的 XY 模式与当前连接不同，请重新选择模式并连接")
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
            if hasattr(self.device, "clear_rpc_trace"):
                self.device.clear_rpc_trace()
            try:
                # Capability checks are read-only. Missing optional services do
                # not disable the existing angle-scanning capability.
                self.device.read_z()
                self.device.read_optics()
                records = MarkRecords(self.root, output, recipe, layers, self.simulated)
                starting = True
                self.mark_run_path, self.mark_output_path = records.path, records.output
                self.result = None
                self.result_path = None
                capture_marks(self.device, recipe, layers, self._stop, records,
                              lambda detail: self._state("对位采图", detail=detail))
                records.document["rpc_trace"] = self.device.rpc_trace() if hasattr(self.device, "rpc_trace") else []
                records.document["operator_experiment_loading_confirmed"] = experiment_loading_confirmed
                records.finish("complete", recovery=False)
                final = "下 mark" if layers[-1] == "lower" else "上 mark"
                self._state("就绪", detail=f"采图完成，停留在{final}焦面；图片目录：{records.output}")
            except Exception as error:
                stopped = isinstance(error, (CaptureStopped, InterruptedError)) or self._stop.is_set()
                detail = "已停止后续拍摄" if stopped else self._mark_hint(error)
                if records is not None:
                    from zen_hardware.rpc_diagnostics import diagnostic_from_error
                    records.document["error"] = diagnostic_from_error(error)
                    records.document["rpc_trace"] = self.device.rpc_trace() if hasattr(self.device, "rpc_trace") else []
                    records.document["operator_experiment_loading_confirmed"] = experiment_loading_confirmed
                    try:
                        records.finish("stopped" if stopped else "failed", recovery=True,
                                       stage=records.document["status"]["failure_stage"] + " · " + detail)
                    except OSError:
                        detail = "图像或状态写入失败，请检查运行记录与输出目录"
                # A run record may exist even when starting is False: MarkRecords
                # writes its fail-closed status before creating the output dir.
                recovery_open = starting or bool(pending_recovery(self.root, simulated=self.simulated))
                if recovery_open:
                    self._state("设备状态待确认", detail=detail + "；请确认 XY、Z、AF 与拍摄均已结束")
                else:
                    self._state(previous, detail=detail)
                from zen_hardware.rpc_diagnostics import diagnostic_from_error
                from zen_records import utc_now
                self.mark_diagnostic_report = {
                    "kind": "capture_failure", "checked_at": utc_now(), "simulated": self.simulated,
                    "error": diagnostic_from_error(error) or {"message": detail},
                    "rpc_trace": self.device.rpc_trace() if hasattr(self.device, "rpc_trace") else [],
                    "recovery_required": recovery_open, "device_actions_possible": recovery_open,
                }
                self.events.put(("mark_diagnostic", self.mark_diagnostic_report))
                self.events.put(("mark_error", detail))
        self._launch(action)

    def inspect_mark_experiments(self, names, *, reported_zen_version=""):
        if self.state == "未连接":
            raise RuntimeError("请先连接 Gateway；此检查不会加载实验或动作")
        def action():
            if hasattr(self.device, "clear_rpc_trace"):
                self.device.clear_rpc_trace()
            try:
                report = self.device.inspect_mark_experiments(tuple(names))
            except Exception as error:
                from zen_hardware.rpc_diagnostics import diagnostic_from_error
                report = {"kind": "read_only_experiment_check", "device_actions_performed": False,
                          "errors": [diagnostic_from_error(error) or {"message": self._mark_hint(error)}]}
            from zen_records import utc_now
            report.update({"checked_at": utc_now(), "simulated": self.simulated,
                           "reported_zen_version": reported_zen_version, "version_source": "operator"})
            self.mark_diagnostic_report = report
            self.events.put(("mark_diagnostic", report))
        self._launch(action)

    def run_hardware_test(self, session, operation, parameters, *, confirmed=False, timeout_s=60):
        """Use the same device task gate as both production measurement flows."""
        if session.library is None or session.library.device is not self.device:
            raise ValueError("测试会话与当前设备不一致，请重新绑定")
        def action():
            try:
                report=session.execute(operation,parameters,confirmed=confirmed,timeout_s=timeout_s,stop=self._stop)
            except Exception as error:
                from zen_hardware.rpc_diagnostics import diagnostic_from_error
                from zen_records import safe
                report={'operation':operation,'parameters':safe(parameters),'status':'failed','writes':True,
                        'recovery_required':True,'result':None,
                        'note':'单步调用未按预期返回；须现场确认设备动作已结束',
                        'error':diagnostic_from_error(error) or {'code':type(error).__name__,
                            'message':str(error) or '调用失败，请检查接口与日志目录'}}
            if report['recovery_required']:
                self._state("设备状态待确认",detail="硬件单步已返回或失败；须现场核对动作结束，日志中保留实际结果")
            elif operation=='disconnect':
                self._state("未连接",detail="客户端通道已断开，不代表物理动作已停止")
            elif operation=='connect' and report['status']=='success':
                self._state("通信就绪（硬件单步）",detail="仅通道已连接；认证/位置/业务就绪须分别验证")
            elif report['writes']:
                self._state("通信就绪（硬件单步）",detail="参数已操作；业务采集前请重新连接并校验")
            self.events.put(("hardware_result",report))
        self._launch(action)
