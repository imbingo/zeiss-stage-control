"""Optional Focus v2 and raw-snap capabilities for the ZEN adapter."""

from math import isclose, isfinite
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic

from zen_images import RawFrame


class MarkCapabilityError(RuntimeError):
    """A required mark-capture service is not exposed by this device."""


class GatewayMarkMixin:
    def _mark_stub(self, module, service):
        from importlib import import_module
        if self._channel is None:
            raise RuntimeError("请先连接 Gateway")
        return getattr(import_module(module), service)(self._channel)

    def read_z(self):
        import grpc
        from zen_api.lm.hardware.v2.focus_service_pb2 import FocusServiceGetPositionRequest
        stub = self._mark_stub('zen_api.lm.hardware.v2.focus_service_pb2_grpc', 'FocusServiceStub')
        try:
            value = stub.GetPosition(FocusServiceGetPositionRequest(), timeout=self._timeout).value
        except grpc.RpcError as error:
            if error.code() == grpc.StatusCode.UNIMPLEMENTED:
                raise MarkCapabilityError("现场未提供 Focus v2，无法自动读取/移动 Z；不会回退到单位不同的 Focus v1") from None
            raise
        if not isfinite(value):
            raise ValueError("Focus v2 返回无效 Z")
        return value  # v2 is METRES. Do not fall back to the micrometre v1 service.

    def read_optics(self):
        from zen_api.lm.hardware.v2.objective_changer_service_pb2 import ObjectiveChangerServiceGetPositionRequest
        from zen_api.lm.hardware.v2.optovar_service_pb2 import OptovarServiceGetPositionRequest
        import grpc
        values = []
        for module, service, request in (
            ('objective_changer_service', 'ObjectiveChangerServiceStub', ObjectiveChangerServiceGetPositionRequest),
            ('optovar_service', 'OptovarServiceStub', OptovarServiceGetPositionRequest),
        ):
            stub = self._mark_stub('zen_api.lm.hardware.v2.'+module+'_pb2_grpc', service)
            try:
                values.append(stub.GetPosition(request(), timeout=self._timeout).value)
            except grpc.RpcError as error:
                if not self.xy_enabled and error.code() == grpc.StatusCode.UNIMPLEMENTED:
                    values.append(None)  # Fixed/manual optics: explicitly unverified.
                else:
                    raise
        return tuple(values)

    def move_z(self, z_m):
        from zen_api.lm.hardware.v2.focus_service_pb2 import FocusServiceMoveToRequest
        if not isfinite(z_m):
            raise ValueError("Z 目标无效")
        self._mark_stub('zen_api.lm.hardware.v2.focus_service_pb2_grpc', 'FocusServiceStub').MoveTo(
            FocusServiceMoveToRequest(value=z_m), timeout=self._timeout)

    def prepare_mark_experiment(self, name, low_m, high_m, *, use_autofocus=True):
        from google.protobuf.wrappers_pb2 import BoolValue, DoubleValue
        from zen_api.acquisition.v1beta.experiment_service_pb2 import ExperimentServiceCloneRequest
        from zen_api.lm.acquisition.v1.experiment_sw_autofocus_service_pb2 import (
            ExperimentSwAutofocusServiceGetAutofocusParametersRequest as Get,
            ExperimentSwAutofocusServiceSetAutofocusParametersRequest as Set,
        )
        if not all(map(isfinite, (low_m, high_m))) or low_m >= high_m:
            raise ValueError("AF 范围无效")
        self.load_experiment(name)
        clone = self._experiments.Clone(ExperimentServiceCloneRequest(experiment_id=self._experiment_id), timeout=self._timeout)
        if not clone.experiment_id or clone.experiment_id == self._experiment_id:
            raise ValueError("未能取得独立的临时实验，拒绝修改原实验")
        self._experiment_id = clone.experiment_id
        if not use_autofocus:
            return {"experiment_id": self._experiment_id, "autofocus": False}
        params = self._af.GetAutofocusParameters(Get(experiment_id=self._experiment_id), timeout=self._timeout)
        if params.auto_focus_mode not in (1, 2, 3):
            raise ValueError("采图实验需使用软件对比度／强度 AF，不支持反射式 AF")
        self._af.SetAutofocusParameters(Set(
            experiment_id=self._experiment_id, autofocus_mode=params.auto_focus_mode,
            contrast_measure=params.contrast_measure, search_strategy="Full",
            autofocus_sampling=params.autofocus_sampling,
            relative_range_is_automatic=BoolValue(value=False),
            relative_search_range=DoubleValue(value=high_m-low_m),
            lower_limit=DoubleValue(value=low_m), upper_limit=DoubleValue(value=high_m),
        ), timeout=self._timeout)
        actual = self._af.GetAutofocusParameters(Get(experiment_id=self._experiment_id), timeout=self._timeout)
        if (actual.relative_range_is_automatic or actual.search_strategy.casefold() != 'full'
                or not isclose(actual.lower_limit, low_m, rel_tol=0, abs_tol=1e-12)
                or not isclose(actual.upper_limit, high_m, rel_tol=0, abs_tol=1e-12)
                or not isclose(actual.relative_search_range, high_m-low_m, rel_tol=0, abs_tol=1e-12)):
            raise ValueError("设备未接受所选层的限定 AF 范围，已阻止 Z 移动")
        return {"experiment_id": self._experiment_id, "lower_m": actual.lower_limit,
                "upper_m": actual.upper_limit, "mode": actual.auto_focus_mode,
                "search_strategy": actual.search_strategy}

    def capture_frame(self, timeout_s, stop, output_name, *, kind="camera_snap"):
        import grpc
        from google.protobuf.wrappers_pb2 import Int32Value
        from zen_api.acquisition.v1beta.experiment_service_pb2 import (
            ExperimentServiceRunSnapRequest, ExperimentServiceRunExperimentRequest,
            ExperimentServiceGetStatusRequest, ExperimentServiceStopRequest, ExperimentServiceCloneRequest,
        )
        from zen_api.acquisition.v1beta.experiment_streaming_service_pb2 import ExperimentStreamingServiceMonitorExperimentRequest

        if kind not in ("camera_snap", "confocal"):
            raise ValueError("未知的采集方式")
        if not self._experiment_id:
            raise RuntimeError("请先准备临时拍摄实验")
        # Use another clone exclusively for the exposure: the AF experiment's
        # final/buffered frame must never be accepted as the requested photograph.
        af_id = self._experiment_id
        capture = self._experiments.Clone(ExperimentServiceCloneRequest(experiment_id=af_id), timeout=self._timeout)
        if not capture.experiment_id or capture.experiment_id == af_id:
            raise ValueError("未能创建独立拍摄实验，拒绝接收可能属于 AF 的旧帧")
        experiment_id = self._experiment_id = capture.experiment_id
        streaming = self._mark_stub('zen_api.acquisition.v1beta.experiment_streaming_service_pb2_grpc', 'ExperimentStreamingServiceStub')
        # Subscribe BEFORE triggering this single exposure. A unique clone ID
        # prevents a live/previous/AF frame from being mistaken for this snap.
        stream = streaming.MonitorExperiment(ExperimentStreamingServiceMonitorExperimentRequest(
            experiment_id=experiment_id, channel_index=Int32Value(value=0), enable_raw_data=False,
        ), timeout=timeout_s)
        messages, started = Queue(), Event()
        def receive():
            started.set()
            try:
                for reply in stream:
                    if reply.frame_data.experiment_id == experiment_id:
                        messages.put(reply.frame_data)
            except Exception as error:
                messages.put(error)
        reader = Thread(target=receive, daemon=True)
        reader.start()
        started.wait()
        snap, frame = None, None
        deadline = monotonic() + timeout_s
        try:
            if stop.is_set():
                raise InterruptedError("已请求停止拍摄")
            # Confocal must execute the saved acquisition experiment. Never fall
            # back to the camera/localisation Snap endpoint on an error.
            method = self._experiments.RunExperiment if kind == 'confocal' else self._experiments.RunSnap
            request = ExperimentServiceRunExperimentRequest if kind == 'confocal' else ExperimentServiceRunSnapRequest
            snap = method.future(request(experiment_id=experiment_id, output_name=output_name), timeout=timeout_s)
            while True:
                if stop.is_set():
                    raise InterruptedError("已请求停止拍摄")
                if monotonic() >= deadline:
                    raise TimeoutError("单张拍摄或完整图像接收超时，不会自动重拍")
                try:
                    item = messages.get(timeout=.02)
                except Empty:
                    item = None
                if isinstance(item, Exception):
                    # Some servers end the stream with the snap. A received full
                    # frame may still be valid, but the snap RPC must also succeed.
                    if frame is None:
                        raise item
                elif item is not None:
                    if frame is not None:
                        raise ValueError("一次单张拍摄返回多帧，拒绝不明确的图像")
                    frame = item
                if snap.done():
                    snap.result()  # Preserve the actual acquisition error.
                    if frame is not None:
                        break
            if kind == 'confocal':
                while True:
                    if stop.is_set():
                        raise InterruptedError("已请求停止共聚焦采集")
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError("共聚焦实验结束状态未确认，未保存图像")
                    status = self._experiments.GetStatus(ExperimentServiceGetStatusRequest(
                        experiment_id=experiment_id), timeout=min(self._timeout, remaining)).status
                    counts = (status.images_count, status.channels_count, status.zstack_slices_count,
                              status.time_points_count, status.scenes_count, status.cumulated_tiles_count, status.tiles_count)
                    if any(value > 1 for value in counts):
                        raise ValueError("共聚焦实验必须是单通道、单 Z 层、单时间点、无拼图的一张图像")
                    if not status.is_experiment_running and not status.is_acquisition_running and status.images_count == 1:
                        break
                    stop.wait(.05)
            p, f = frame.pixel_data, frame.frame_position
            if (p.start_position.x or p.start_position.y
                    or (p.size.width, p.size.height) != (frame.frame_size.width, frame.frame_size.height)
                    or f.c != 0 or any((f.z, f.t, f.s, f.m, f.h))):
                raise ValueError("需要单通道单帧的完整图像；收到部分帧或多维采集")
            position = frame.frame_stage_position
            if self.xy_enabled and not all(position.HasField(key) for key in ('x', 'y', 'z')):
                raise ValueError("原始图像未提供完整的 XYZ 位置")
            z_source = 'frame' if position.HasField('z') else 'focus_readback_after_capture'
            z = position.z.value if position.HasField('z') else self.read_z()
            xy = (position.x.value, position.y.value) if self.xy_enabled else (None, None)
            result = RawFrame(frame.frame_size.width, frame.frame_size.height, p.pixel_type,
                              p.raw_data, frame.scaling.x, frame.scaling.y, (*xy, z),
                              experiment_id, frame.frame_number, (f.x, f.y), f.c,
                              self.xy_enabled, z_source, kind)
            result.array()
            return result
        except BaseException as error:
            if snap is not None:
                snap.cancel()
                try:
                    self._experiments.Stop(ExperimentServiceStopRequest(experiment_id=experiment_id), timeout=self._timeout)
                except Exception:
                    pass  # Recovery remains locked even when Stop is accepted.
            if isinstance(error, grpc.RpcError) and error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise TimeoutError("单张拍摄或完整图像接收超时，不会自动重拍") from error
            raise
        finally:
            stream.cancel()
            reader.join(timeout=1)
