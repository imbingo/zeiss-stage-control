"""ZEN 2026.11.1 adapter using official generated Python gRPC modules.

The channel factory owns TLS and token configuration. It must return a
secure, authenticated channel with retries disabled for device commands.
"""

from . import AutofocusTimeout, DeviceDisconnected, Motion
from .mark_gateway import GatewayMarkMixin
from .rpc_diagnostics import RpcDiagnosticsMixin
from .controls import GatewayControlsMixin


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
            grpc.StatusCode.PERMISSION_DENIED: "Gateway 拒绝该调用。请启用 Unsupervised API Mode（ZEN：Tools → Options → ZEN API；ZEN core：主页 API Mode），确认 ZEN API toolkit 已激活，再核对令牌是否来自当前 Gateway。",
            grpc.StatusCode.UNIMPLEMENTED: "当前 ZEN/Gateway 未注册该方法。请核对 ZEN 版本与本软件所需 API（2026.11.1）是否匹配，以及 ZEN API toolkit 是否已激活。",
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


class GatewayDevice(GatewayMarkMixin, GatewayControlsMixin, RpcDiagnosticsMixin):
    def __init__(self, channel_factory, *, rpc_timeout=5.0, xy_enabled=True,
                 allow_focus_v1=False, allow_unverified_af=False,
                 allow_definite_focus=False):
        self.clear_rpc_trace()
        self.xy_enabled = xy_enabled
        # Explicit opt-in only: Focus v1 reports micrometres where v2 reports
        # metres. Never enabled silently; see read_z/move_z.
        self.allow_focus_v1 = allow_focus_v1
        # Explicit opt-in only: when neither AF parameter read nor write exists,
        # FindAutoFocus would run the experiment's stored search range which we
        # cannot verify. Never enabled silently.
        self.allow_unverified_af = allow_unverified_af
        # Explicit opt-in only: DefiniteFocusService is a different engine
        # (infrared-reflection surface detection, ZEN-bounded range). It is the
        # fallback when the software AF service is absent; never silent.
        self.allow_definite_focus = allow_definite_focus
        from .capability import CapabilityMatrix
        self.capabilities = CapabilityMatrix()
        self._channel_factory = channel_factory
        self._timeout = rpc_timeout
        self._channel = None
        self._experiment_id = None

    @property
    def connected(self):
        return self._channel is not None

    def close(self):
        channel, self._channel = self._channel, None
        self._experiment_id = None
        if channel is not None:
            channel.close()

    def connect(self):
        self.connect_transport()
        try:
            self.probe_services()
            return self.read_xy() if self.xy_enabled else self.read_z()
        except Exception:
            self.close()
            raise

    def connect_transport(self):
        """TLS/channel only: no experiment, position, motion or acquisition RPC."""
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
        except grpc.FutureTimeoutError:
            self.close()
            raise DeviceDisconnected("Gateway TLS 连接超时") from None
        except Exception:
            self.close()
            raise

    def probe_services(self, timeout=3.0):
        """List Gateway-registered services into the capability matrix.

        Read-only and never raises: a Gateway without reflection support
        simply leaves the matrix service-blind. Candidate services that are
        absent mark their methods unsupported so they fail fast afterwards.
        """
        if self._channel is None:
            return None
        from .reflection import OPTIONAL_SERVICES, REQUIRED_SERVICES, list_services
        try:
            names = list_services(self._channel, timeout=timeout)
        except Exception:
            return None
        candidates = set(REQUIRED_SERVICES["motorized" if self.xy_enabled else "no_xy"])
        candidates.update(OPTIONAL_SERVICES)
        self.capabilities.note_services(names, candidates)
        return names

    def read_xy(self):
        if not self.xy_enabled:
            raise RuntimeError("无 XY 单点采图模式不提供 XY 操作")
        from zen_api.hardware.v1.simple_stage_service_pb2 import SimpleStageServiceGetPositionRequest

        reply = self._rpc("zen_api.hardware.v1.SimpleStageService.GetPosition", self._xy.GetPosition,
                          SimpleStageServiceGetPositionRequest(), timeout=self._timeout)
        return reply.x, reply.y

    def motion_state(self):
        if not self.xy_enabled:
            raise RuntimeError("无 XY 单点采图模式不提供 XY 操作")
        from zen_api.hardware.v1.stage_service_pb2 import StageServiceGetStageMotionStateRequest
        from zen_api.hardware.v1 import stage_motion_state_pb2 as states

        reply = self._rpc("zen_api.hardware.v1.StageService.GetStageMotionState", self._stage.GetStageMotionState,
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

        self._rpc("zen_api.hardware.v1.SimpleStageService.MoveTo", self._xy.MoveTo,
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
            if exc.code() == grpc.StatusCode.UNIMPLEMENTED and self.allow_definite_focus:
                # Software AF service absent (ZEN 3.12): hardware Definite
                # Focus finds the surface within its ZEN-configured range.
                return self.definite_focus_surface(timeout_seconds)
            raise
        return reply.focus_position
