"""消息构造辅助（替换 langchain.schema 消息类）。

P7 决策：彻底移除 langchain。对话消息统一为 OpenAI 兼容 dict 形态
{"role": "system|user|assistant", "content": str}——与 openai SDK
messages 参数天然一致，各调用点无需再转换。
"""

from __future__ import annotations

from typing import Dict, List, Union

Message = Dict[str, str]


def user(content: str) -> Message:
    return {"role": "user", "content": content}


def assistant(content: str) -> Message:
    return {"role": "assistant", "content": content}


def system(content: str) -> Message:
    return {"role": "system", "content": content}


def as_messages(items: List[Union[Message, "History"]]) -> List[Message]:
    """把 History 列表/消息 dict 列表归一为 OpenAI 消息 dict 列表。"""
    out: List[Message] = []
    for item in items:
        if isinstance(item, dict) and "role" in item and "content" in item:
            out.append({"role": item["role"], "content": item["content"]})
        else:
            # History 对象（role: user/assistant/human/ai）
            role = getattr(item, "role", "user")
            if role in ("human", "ai"):
                role = "user" if role == "human" else "assistant"
            out.append({"role": role, "content": str(getattr(item, "content", ""))})
    return out
