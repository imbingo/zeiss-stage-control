"""Resolve documented local Gateway defaults without persisting secrets."""

from pathlib import Path
import os


def gateway_sources(env):
    address = env.get("ZEN_GATEWAY_ADDRESS") or "127.0.0.1:5000"
    root = Path(env.get("PROGRAMDATA") or r"C:\ProgramData") / "Carl Zeiss" / "ZEN APIGateway"
    local = address.rpartition(":")[0] in {"localhost", "127.0.0.1", "[::1]"}
    certificate = env.get("ZEN_GATEWAY_CA")
    token = env.get("ZEN_GATEWAY_TOKEN")
    pem = Path(certificate) if certificate else (
        root / "Certificates" / "ZEN APIPersonalSigningRootCA.pem" if local else None
    )
    token_file = root / "GlobalControlToken.txt" if local else None
    return address, pem, token, token_file


def read_control_token(token, token_file):
    if not token and token_file is not None:
        token = token_file.read_text(encoding="utf-8-sig").strip()
    if not token or not token.isascii() or any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError("控制令牌缺失或格式无效")
    return token


def gateway_settings(env=os.environ):
    address, pem, token, token_file = gateway_sources(env)
    if pem is None or not pem.is_file():
        raise ValueError("找不到 Gateway CA 证书。请确认已安装 Gateway，或配置 ZEN_GATEWAY_CA")
    try:
        token = read_control_token(token, token_file)
    except (OSError, ValueError):
        raise ValueError("无法读取有效控制令牌。请从 Gateway 托盘复制令牌到连接设置") from None
    return address, pem, token
