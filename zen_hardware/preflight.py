"""Read-only startup checks for the configured ZEN Gateway."""

from math import isfinite
import ssl

from . import Motion
from .gateway import GatewayDevice, connection_hint, secure_channel
from .local_config import gateway_sources, read_control_token


def _report_services(channel, report, xy_enabled):
    """List Gateway-registered services via reflection and diff against needs."""
    from .reflection import OPTIONAL_SERVICES, REQUIRED_SERVICES, list_services, short_name

    try:
        names = list_services(channel, timeout=3)
    except ImportError:
        report("services", "— 未完成", "未安装 grpcio-reflection，无法列出 Gateway 已注册服务。")
        return
    except Exception as error:
        report("services", "— 未完成", f"无法列出服务清单：{error}；不阻止后续连接检查。")
        return
    if not names:
        report("services", "✕ 失败", "Gateway 未注册任何服务。请确认 ZEN/ZEN core 正在运行并连接 Gateway、ZEN API toolkit 已激活。")
        return
    mode = "motorized" if xy_enabled else "no_xy"
    missing = [short_name(s) for s in REQUIRED_SERVICES[mode] if s not in names]
    optional_missing = [short_name(s) for s in OPTIONAL_SERVICES if s not in names]
    if missing:
        report("services", "✕ 失败",
               f"已注册 {len(names)} 个服务，缺少必需服务：{'、'.join(missing)}。"
               "请核对 ZEN 版本与本软件 API（2026.11.1）是否匹配、toolkit 是否激活。")
    elif optional_missing:
        is_af = lambda s: "AutofocusService" in s or "DefiniteFocusService" in s
        af_missing = [s for s in optional_missing if is_af(s)]
        af_present = [s for s in OPTIONAL_SERVICES if is_af(short_name(s)) and s in names]
        optics_missing = [s for s in optional_missing if s not in af_missing]
        detail = f"已注册 {len(names)} 个服务；缺少可选服务：{'、'.join(optional_missing)}。"
        if af_missing and not af_present:
            detail += (" 全部自动对焦路径（软件 AF 与 Definite Focus）均未注册：mark 采图与角度扫描的"
                       "自动对焦步骤将失败。请在 ZEN 端核对 API toolkit 授权与 ZEN 版本，"
                       "或改用无 AF 手动示教。")
        elif af_missing:
            detail += " 部分 AF 服务未注册，已自动降级到可用路径，不影响对焦。"
        if optics_missing:
            detail += " 物镜/Optovar 读回不可用，仅影响光学参数核对，不影响运动。"
        report("services", "— 待确认", detail)
    else:
        report("services", "✓ 通过", f"Gateway 已注册 {len(names)} 个服务，所需接口齐全。")


def check_gateway(env, report, *, xy_enabled=True):
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
        report("services", "— 待检查", "连接建立后才查询 Gateway 已注册服务。")
        report("stage", "— 待检查" if xy_enabled else "— 不适用", "等待读取 XY。" if xy_enabled else "无电动 XY，不查询载物台。")
        return

    channel = secure_channel(address, certificate, token)
    device = GatewayDevice(lambda: channel, rpc_timeout=3, xy_enabled=xy_enabled)
    _report_services(channel, report, xy_enabled)
    try:
        position = device.connect()
        if xy_enabled:
            x, y = position
            if not all(map(isfinite, (x, y))):
                raise ValueError("Invalid XY")
        elif not isfinite(position):
            raise ValueError("Invalid Z")
    except Exception as error:
        if xy_enabled:
            hint = connection_hint(error)
        else:
            from .mark_gateway import MarkCapabilityError
            hint = str(error) if isinstance(error, MarkCapabilityError) else connection_hint(error).replace("XY", "Z")
        report("gateway", "✕ 失败", hint)
        report("stage", "— 待检查" if xy_enabled else "— 不适用", "等待读取 XY。" if xy_enabled else "无电动 XY 模式，不查询载物台。")
    else:
        if not xy_enabled:
            report("gateway", "✓ 通过", f"已通过 TLS/令牌认证读取 Z {position*1e6:.3f} µm；未校验 XY。")
            report("stage", "— 不适用", "无电动 XY，不调用 XY 服务。请手动固定样品；角度扫描禁用。")
            return
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
