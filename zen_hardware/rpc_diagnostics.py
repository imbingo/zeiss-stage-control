"""Method-level gRPC diagnostics without request bodies, tokens or RPC details."""

from collections import deque
import grpc
from zen_records import utc_now


class GatewayCallError(RuntimeError, grpc.RpcError):
    def __init__(self, method, status):
        self.method = method
        self.status = status
        hint = {
            grpc.StatusCode.UNIMPLEMENTED: "当前 ZEN/Gateway 未实现或未注册此方法，不会跳过或自动换接口；请核对 ZEN 版本、API toolkit 是否激活，以及该机型是否提供此服务",
            grpc.StatusCode.NOT_FOUND: "指定的实验或对象不存在，请核对名称；这不等于接口未实现",
            grpc.StatusCode.UNAUTHENTICATED: "认证失败，请核对当前 Gateway 控制令牌",
            grpc.StatusCode.PERMISSION_DENIED: "控制类调用被拒绝。请启用 Unsupervised API Mode（ZEN：Tools → Options → ZEN API；ZEN core：主页 API Mode 或 Maintenance → General Options → ZEN API），并确认 ZEN API toolkit 已激活",
            grpc.StatusCode.UNAVAILABLE: "服务不可用，请核对 ZEN 注册状态与 Gateway 转发",
            grpc.StatusCode.DEADLINE_EXCEEDED: "调用超时；若已涉及设备动作，须现场确认动作结束",
        }.get(status, "接口调用失败，请保留诊断记录")
        super().__init__(f"{method} → {status.name}：{hint}")

    def code(self):
        return self.status

    def details(self):
        return str(self)

    def to_dict(self):
        return {"method": self.method, "code": self.status.name, "message": str(self)}


def diagnostic_from_error(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, GatewayCallError):
            return error.to_dict()
        error = error.__cause__ or error.__context__
    return None


class RpcDiagnosticsMixin:
    def clear_rpc_trace(self):
        self._rpc_trace = deque(maxlen=256)

    def rpc_trace(self):
        return list(getattr(self, '_rpc_trace', ()))

    def _rpc(self, method, call, request, **kwargs):
        try:
            result = call(request, **kwargs)
        except grpc.RpcError as error:
            wrapped = self._rpc_failure(method, error)
            raise wrapped from error
        self._rpc_record(method, 'OK')
        return result

    def _rpc_failure(self, method, error):
        if isinstance(error, GatewayCallError):
            return error
        status = error.code() or grpc.StatusCode.UNKNOWN
        self._rpc_record(method, status.name)
        return GatewayCallError(method, status)

    def _rpc_record(self, method, code):
        if not hasattr(self, '_rpc_trace'):
            self.clear_rpc_trace()
        self._rpc_trace.append({"at": utc_now(), "method": method, "code": code})
