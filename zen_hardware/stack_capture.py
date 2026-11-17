"""Complete multi-plane confocal stream. Never collapse or silently flatten Z."""

from queue import Queue, Empty, Full
from threading import Event, Thread
from time import monotonic
from math import isfinite, isclose
from zen_images import RawFrame


def capture_stack(device, first, last, interval, timeout, stop, output_name):
    import grpc
    from google.protobuf.wrappers_pb2 import Int32Value
    from zen_api.acquisition.v1beta.experiment_service_pb2 import (
        ExperimentServiceRunExperimentRequest,
        ExperimentServiceGetStatusRequest,
        ExperimentServiceStopRequest,
    )
    from zen_api.acquisition.v1beta.experiment_streaming_service_pb2 import (
        ExperimentStreamingServiceMonitorExperimentRequest,
    )

    if (
        not all(isfinite(v) for v in (first, last, interval, timeout))
        or interval <= 0
        or timeout <= 0
    ):
        raise ValueError("Z-stack 参数无效")
    count = round(abs(last - first) / interval) + 1
    if not 2 <= count <= 4096 or not isclose(
        (count - 1) * interval, abs(last - first), rel_tol=1e-6, abs_tol=1e-12
    ):
        raise ValueError("Z-stack 层数或间距无效")
    eid = getattr(device, "_experiment_id", None)
    if not eid:
        raise ValueError("请先加载实验")
    source = eid
    device.call_hardware_operation("clone_experiment", {"experiment_id": eid})
    eid = device._experiment_id
    if not eid or eid == source:
        raise ValueError("未获得独立扫描实例")
    if not device.call_hardware_operation("is_zstack", {"experiment_id": eid}).get(
        "is_zstack_experiment"
    ):
        raise ValueError("ZEN 实验尚未启用 Z-stack 维度，请在 ZEN 配置后重新加载")
    device.call_hardware_operation(
        "set_zstack",
        {
            "experiment_id": eid,
            "first_um": first * 1e6,
            "last_um": last * 1e6,
            "interval_um": interval * 1e6,
        },
    )
    actual = device.call_hardware_operation("get_zstack", {"experiment_id": eid})
    for key, target in [
        ("first_slice", first),
        ("last_slice", last),
        ("interval", interval),
    ]:
        value = actual.get(key)
        if isinstance(value, dict):
            value = value.get("value")
        if value is None or not isclose(
            float(value), target, rel_tol=1e-6, abs_tol=1e-12
        ):
            raise ValueError("Z-stack 参数未有效读回：" + key)
    stream_stub = device._mark_stub(
        "zen_api.acquisition.v1beta.experiment_streaming_service_pb2_grpc",
        "ExperimentStreamingServiceStub",
    )
    stream = stream_stub.MonitorExperiment(
        ExperimentStreamingServiceMonitorExperimentRequest(
            experiment_id=eid, channel_index=Int32Value(value=0), enable_raw_data=False
        ),
        timeout=timeout,
    )
    messages = Queue(maxsize=8)
    closing = Event()
    ready = Event()
    future = None
    frames = {}
    total = 0
    deadline = monotonic() + timeout

    def enqueue(item):
        while not closing.is_set():
            try:
                messages.put(item, timeout=0.05)
                return
            except Full:
                pass

    def receive():
        ready.set()
        try:
            for message in stream:
                if message.frame_data.experiment_id == eid:
                    enqueue(message.frame_data)
        except Exception as e:
            if not closing.is_set():
                enqueue(e)

    reader = Thread(target=receive, daemon=True)
    reader.start()
    ready.wait()
    try:
        if stop.is_set():
            raise InterruptedError("已停止 Z-stack")
        future = device._experiments.RunExperiment.future(
            ExperimentServiceRunExperimentRequest(
                experiment_id=eid, output_name=output_name
            ),
            timeout=timeout,
        )
        done = False
        while True:
            if stop.is_set():
                raise InterruptedError("已停止 Z-stack 后续操作")
            if monotonic() >= deadline:
                raise TimeoutError("Z-stack 采集或完整帧接收超时")
            try:
                message = messages.get(timeout=0.02)
            except Empty:
                message = None
            if isinstance(message, Exception):
                if len(frames) < count:
                    if isinstance(message, grpc.RpcError):
                        raise device._rpc_failure(
                            "zen_api.acquisition.v1beta.ExperimentStreamingService.MonitorExperiment",
                            message,
                        ) from message
                    raise message
            elif message is not None:
                p = message.pixel_data
                f = message.frame_position
                stage = message.frame_stage_position
                if f.c or any((f.t, f.s, f.m, f.h)) or not 0 <= f.z < count:
                    raise ValueError("Z-stack 收到额外通道/时间/拼图或无效 Z 索引")
                if f.z in frames:
                    raise ValueError("Z-stack 收到重复层，拒绝不明确的数据")
                if (
                    p.start_position.x
                    or p.start_position.y
                    or (p.size.width, p.size.height)
                    != (message.frame_size.width, message.frame_size.height)
                ):
                    raise ValueError("Z-stack 未收到完整平面")
                if not stage.HasField("z"):
                    raise ValueError("Z-stack 图像缺少每层实际 Z 位置")
                if device.xy_enabled and not all(stage.HasField(k) for k in ("x", "y")):
                    raise ValueError("Z-stack 缺少实际 XY")
                xy = (
                    (stage.x.value, stage.y.value)
                    if device.xy_enabled
                    else (None, None)
                )
                frame = RawFrame(
                    message.frame_size.width,
                    message.frame_size.height,
                    p.pixel_type,
                    p.raw_data,
                    message.scaling.x,
                    message.scaling.y,
                    (*xy, stage.z.value),
                    eid,
                    message.frame_number,
                    (f.x, f.y),
                    0,
                    device.xy_enabled,
                    "frame",
                    "confocal_zstack",
                )
                frame.array()
                total += len(frame.data)
                if total > 512 * 1024 * 1024:
                    raise ValueError("Z-stack 超过 512 MiB 接收上限，请缩小 ROI 或层数")
                frames[f.z] = frame
            if future.done() and not done:
                try:
                    future.result()
                except grpc.RpcError as error:
                    raise device._rpc_failure(
                        "zen_api.acquisition.v1beta.ExperimentService.RunExperiment",
                        error,
                    ) from error
                device._rpc_record(
                    "zen_api.acquisition.v1beta.ExperimentService.RunExperiment", "OK"
                )
                done = True
            if done and len(frames) == count:
                status = device._rpc(
                    "zen_api.acquisition.v1beta.ExperimentService.GetStatus",
                    device._experiments.GetStatus,
                    ExperimentServiceGetStatusRequest(experiment_id=eid),
                    timeout=min(device._timeout, max(0.01, deadline - monotonic())),
                ).status
                if any(
                    v > 1
                    for v in (
                        status.channels_count,
                        status.time_points_count,
                        status.scenes_count,
                        status.tiles_count,
                        status.cumulated_tiles_count,
                    )
                ):
                    raise ValueError("Z-stack 要求单通道、无时间序列及拼图")
                if (
                    not status.is_experiment_running
                    and not status.is_acquisition_running
                ):
                    if (
                        status.zstack_slices_count != count
                        or status.images_count != count
                    ):
                        raise ValueError("Z-stack 实际采集计数不符")
                    break
                stop.wait(0.02)
        while not messages.empty():
            if not isinstance(messages.get_nowait(), Exception):
                raise ValueError("Z-stack 有额外未确认图像")
        return [frames[i] for i in range(count)]
    except BaseException:
        if future is not None:
            future.cancel()
            try:
                device._rpc(
                    "zen_api.acquisition.v1beta.ExperimentService.Stop",
                    device._experiments.Stop,
                    ExperimentServiceStopRequest(experiment_id=eid),
                    timeout=device._timeout,
                )
            except Exception:
                pass
        raise
    finally:
        closing.set()
        stream.cancel()
        reader.join(timeout=1)
