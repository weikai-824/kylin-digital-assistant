"""SSE / OpenAI 兼容输出格式封装。

从 rag_chat 中拆出：chunk/完成/错误消息的构造与序列化统一收敛，避免
各生成路径各写一份 OpenAIChatOutput 拼装。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

from kylin.platform.runtime.schemas import OpenAIChatOutput


def chat_id() -> str:
    return f"chat{uuid.uuid4()}"


def make_chat_output(session_id: Optional[str] = None, **kw: Any) -> OpenAIChatOutput:
    """构造 OpenAI 兼容输出；session_id 通过 extra 字段透出（前端用于会话恢复）。"""
    if session_id:
        kw["session_id"] = session_id
    return OpenAIChatOutput(**kw)


def sse_error(err: BaseException) -> Dict[str, str]:
    """SSE 错误事件：data: {"error": "..."}（ensure_ascii=False 保中文可读）。"""
    return {"data": json.dumps({"error": str(err)}, ensure_ascii=False)}
