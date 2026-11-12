"""ZEN 2026.11.1 adapter using official generated Python gRPC modules.

The channel factory owns TLS and token configuration. It must return a
secure, authenticated channel with retries disabled for device commands.
"""

from . import AutofocusTimeout, DeviceDisconnected, Motion
from .mark_gateway import GatewayMarkMixin
from .rpc_diagnostics import RpcDiagnosticsMixin


def connection_hint(error):
    """Return operator guidance without exposing RPC details or credentials."""
    if isinstance(error, ImportError):
        return "缺少 Gateway 依赖，请从完整 Windows 便携包启动"
    if isinstance(error, OSError) and not isinstance(error, DeviceDisconnected):
        return "无法读取 CA 证书，请检查文件路径和权限"
    if isinstance(error, DeviceDisconnected):
        return ("Gateway 连接超时。先确认托盘中的 Gateway 已启动，并核对客户端地址和监听端口。"
                "若 Gateway 无法启动，请检查端口是否被占用或保留。端口正确时，再核对主机名和 Gateway 的 CA 证书。")
    if isinstance(error, ValueError):
        return "Gateway 配置或 XY 读数无效，请检查证书、令牌和载物台"
    try:
        import grpc
    except ImportError:
        return "缺少 Gateway 依赖，请从完整 Windows 便携包启动"
    if isinstance(error, grpc.RpcError):
        return {
            grpc.StatusCode.UNAUTHENTICATED: "Gateway 认证失败。从当前 Gateway 的托盘菜单复制全局控制令牌，填入连接设置后重试。",
            grpc.StatusCode.PERMISSION_DENIED: "Gateway 拒绝只读请求。先核对连接设置中的令牌是否来自当前 Gateway，再确认 ZEN API 工具包已启用。",
            grpc.StatusCode.UNIMPLEMENTED: "当前服务未提供该 XY 方法。核对 ZEN 版本、设备和当前配置是否支持该方法。",
            grpc.StatusCode.UNAVAILABLE: "请启动 ZEN 或 ZEN Core。将鼠标移到 Gateway 托盘图标，确认 ZEN 已连接并注册服务，再核对当前配置是否提供 XY 方法。",
            grpc.StatusCode.DEADLINE_EXCEEDED: "XY 读取超时。查看 Gateway 托盘提示，确认 ZEN 正在运行并已注册服务，再重新检查。",
        }.get(error.code(), "XY 服务调用失败，请检查 ZEN 和 Gateway 状态")
    return "连接或状态读取失败，请检查 ZEN 和 Gateway 状态"


def secure_channel(address, certificate, token):
    """Open a TLS channel using the Gateway's documented control-token header."""
    import grpc

    if not address or not certificate or not token or not token.isascii() or any(
        not 33 <= ord(character) <= 126 for character in token
    ):
        raise ValueError("需提供有效的 Gateway 地址、PEM 证书和控制令牌")

    def metadata(context, callback):
        callback((("control-token", token),), None)

    credentials = grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(root_certificates=certificate),
        grpc.metadata_call_credentials(metadata),
    )
    return grpc.secure_channel(address, credentials, options=(("grpc.enable_retries", 0), ("grpc.max_receive_message_length", 256 * 1024 * 1024)))


class GatewayDevice(GatewayMarkMixin, RpcDiagnosticsMixin):
    def __init__(self, channel_factory, *, rpc_timeout=5.0, xy_enabled=True):
        self.clear_rpc_trace()
        self.xy_enabled = xy_enabled
        self._channel_factory = channel_factory
        self._timeout = rpc_timeout
        self._channel = None
        self._experiment_id = None

    def close(self):
        channel, self._channel = self._channel, None
        self._experiment_id = None
        if channel is not None:
            channel.close()

    def connect(self):
        import grpc
        from zen_api.acquisition.v1beta import experiment_service_pb2_grpc
        from zen_api.hardware.v1 import simple_stage_service_pb2_grpc, stage_service_pb2_grpc
        from zen_api.lm.acquisition.v1 import experiment_sw_autofocus_service_pb2_grpc

        self.close()
        self._channel = self._channel_factory()
        if self.xy_enabled:
            self._xy = simple_stage_service_pb2_grpc.SimpleStageServiceStub(self._channel)
            self._stage = stage_service_pb2_grpc.StageServiceStub(self._channel)
        self._experiments = experiment_service_pb2_grpc.ExperimentServiceStub(self._channel)
        self._af = experiment_sw_autofocus_service_pb2_grpc.ExperimentSwAutofocusServiceStub(
            self._channel
        )
        try:
            grpc.channel_ready_future(self._channel).result(timeout=self._timeout)
            return self.read_xy() if self.xy_enabled else self.read_z()
        except grpc.FutureTimeoutError:
            self.close()
            raise DeviceDisconnected("Gateway TLS 连接超时") from None
        except Exception:
            self.close()
            raise

    def read_xy(self):
        if not self.xy_enabled:
            raise RuntimeError("无 XY 单点采图模式不提供 XY 操作")
        from zen_api.hardware.v1.simple_stage_service_pb2 import SimpleStageServiceGetPositionRequest

        reply = self._xy.GetPosition(SimpleStageServiceGetPositionRequest(), timeout=self._timeout)
        return reply.x, reply.y

    def motion_state(self):
        if not self.xy_enabled:
            raise RuntimeError("无 XY 单点采图模式不提供 XY 操作")
        from zen_api.hardware.v1.stage_service_pb2 import StageServiceGetStageMotionStateRequest
        from zen_api.hardware.v1 import stage_motion_state_pb2 as states

        reply = self._stage.GetStageMotionState(
            StageServiceGetStageMotionStateRequest(), timeout=self._timeout
        )
        return {
            states.STAGE_MOTION_STATE_IDLE: Motion.IDLE,
            states.STAGE_MOTION_STATE_MOVING: Motion.MOVING,
            states.STAGE_MOTION_STATE_ERROR: Motion.ERROR,
        }.get(reply.state, Motion.UNKNOWN)

    def move_xy(self, x, y):
        if not self.xy_enabled:
            raise RuntimeError("无 XY 单点采图模式不提供 XY 操作")
        from google.protobuf.wrappers_pb2 import DoubleValue
        from zen_api.hardware.v1.simple_stage_service_pb2 import SimpleStageServiceMoveToRequest

        self._xy.MoveTo(
            SimpleStageServiceMoveToRequest(x=DoubleValue(value=x), y=DoubleValue(value=y)),
            timeout=self._timeout,
        )

    def load_experiment(self, name):
        from zen_api.acquisition.v1beta.experiment_service_pb2 import ExperimentServiceLoadRequest

        self._experiment_id = None
        reply = self._rpc("zen_api.acquisition.v1beta.ExperimentService.Load", self._experiments.Load,
            ExperimentServiceLoadRequest(experiment_name=name), timeout=self._timeout
        )
        if not reply.experiment_id:
            raise RuntimeError("Gateway 未返回已加载的实验 ID")
        self._experiment_id = reply.experiment_id

    def autofocus(self, timeout_seconds):
        import grpc
        from google.protobuf.wrappers_pb2 import DoubleValue
        from zen_api.lm.acquisition.v1.experiment_sw_autofocus_service_pb2 import (
            ExperimentSwAutofocusServiceFindAutoFocusRequest,
        )

        if self._experiment_id is None:
            raise RuntimeError("请先加载实验")
        request = ExperimentSwAutofocusServiceFindAutoFocusRequest(
            experiment_id=self._experiment_id, timeout=DoubleValue(value=timeout_seconds)
        )
        try:
            reply = self._rpc("zen_api.lm.acquisition.v1.ExperimentSwAutofocusService.FindAutoFocus",
                              self._af.FindAutoFocus, request, timeout=timeout_seconds + self._timeout)
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise AutofocusTimeout("AF 调用超时，设备状态待确认") from exc
            if exc.code() == grpc.StatusCode.UNAVAILABLE:
                raise DeviceDisconnected("AF 期间 Gateway 不可用，设备状态待确认") from exc
            raise
        return reply.focus_position
