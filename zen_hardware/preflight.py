"""Read-only startup checks for the configured ZEN Gateway."""

from math import isfinite
import ssl

from . import Motion
from .gateway import GatewayDevice, connection_hint, secure_channel
from .local_config import gateway_sources, read_control_token


def check_gateway(env, report):
    address, pem, token, token_file = gateway_sources(env)
    host, separator, port = address.rpartition(":")
    address_ok = bool(host and separator and port.isascii() and port.isdecimal()
                      and len(port) <= 5 and 1024 <= int(port) <= 65535)
    report("address", "✓ 通过" if address_ok else "✕ 失败",
           address if address_ok else "在连接设置中填写 Gateway 主机名或 IP 和端口。蔡司文档给出的端口范围为 1024–65535。")
    certificate = None
    try:
        if pem is None:
            raise ValueError
        data = pem.read_bytes()
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=data.decode("ascii"))
        certificate = data
    except (OSError, ValueError):
        report("ca", "✕ 失败", r"在连接设置中选择该 Gateway 的 CA PEM。默认目录为 %PROGRAMDATA%\Carl Zeiss\ZEN APIGateway\Certificates。")
    else:
        report("ca", "✓ 通过", str(pem))
    try:
        token = read_control_token(token, token_file)
    except (OSError, ValueError):
        token = None
        report("token", "✕ 失败", "从当前 Gateway 的托盘菜单复制全局控制令牌，填入连接设置。同机也可核对默认 GlobalControlToken.txt 是否可读。")
    else:
        report("token", "✓ 通过", "已读取控制令牌，Gateway 是否接受见下方。")
    if not (address_ok and certificate and token):
        report("gateway", "— 待检查", "修正上方配置后重新检查。")
        report("stage", "— 待检查", "等待读取 XY。")
        return

    device = GatewayDevice(lambda: secure_channel(address, certificate, token), rpc_timeout=3)
    try:
        x, y = device.connect()
        if not all(map(isfinite, (x, y))):
            raise ValueError("Invalid XY")
    except Exception as error:
        report("gateway", "✕ 失败", connection_hint(error))
        report("stage", "— 待检查", "等待读取 XY。")
    else:
        report("gateway", "✓ 通过", f"已通过 TLS 读取 XY  {x*1e6:.3f}, {y*1e6:.3f} µm")
        try:
            motion = device.motion_state()
        except Exception as error:
            report("stage", "✕ 失败", connection_hint(error))
        else:
            report("stage", "✓ 通过" if motion is Motion.IDLE else "— 待确认",
                   "载物台空闲。" if motion is Motion.IDLE else "请在 ZEN 确认载物台状态。")
    finally:
        device.close()
