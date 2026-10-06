"""敏感字段脱敏：日志 / 异常消息输出前的统一净化。

外部接口（OA 等）的错误响应常含 access_token / refresh_token / app_secret /
授权 code，直接拼进异常消息或 logger 会原样落盘到日志文件。本模块提供与结构
无关的递归脱敏，只遮盖**凭证类**字段，保留业务字段（工号 / 部门 / 姓名等），
避免脱敏过度导致线上排查困难。

刻意不脱敏 ``code`` / ``errcode`` / ``message``：它们是排查所需的状态码与
错误描述本身，不含长期凭证。
"""

from __future__ import annotations

from typing import Any

REDACTED = "***"

# 凭证类字段名（一律小写，比对时 key.lower()）
SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "accesstoken",
        "refresh_token",
        "refreshtoken",
        "id_token",
        "token",
        "app_key",
        "appkey",
        "app_secret",
        "appsecret",
        "client_secret",
        "password",
        "passwd",
        "secret",
        "authorization",
    }
)

_MAX_DEPTH = 8


def redact(data: Any, _depth: int = 0) -> Any:
    """递归脱敏：命中 SENSITIVE_KEYS 的**值**整替换为 ``***``。

    仅处理 dict / list / tuple，其余类型原样返回。嵌套超过 8 层直接返回
    ``***``，避免异常深的结构在打日志时拖慢主流程。
    """
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(data, dict):
        return {
            key: (REDACTED if _is_sensitive(key) else redact(value, _depth + 1))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact(item, _depth + 1) for item in data]
    if isinstance(data, tuple):
        return tuple(redact(item, _depth + 1) for item in data)
    return data


def _is_sensitive(key: Any) -> bool:
    return isinstance(key, str) and key.lower() in SENSITIVE_KEYS
