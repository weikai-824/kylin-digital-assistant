"""共享数据模型：History / 响应基类 / OpenAI 兼容输出（共享基础设施）。

这里集中放置 History、响应基类和 OpenAI 兼容输出值对象，供入口适配器和应用层
共同使用；不依赖具体入口或外部服务。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field


class History(BaseModel):
    """
    对话历史
    可从dict生成，如
    h = History(**{"role":"user","content":"你好"})
    也可转换为tuple，如
    h.to_msy_tuple = ("human", "你好")
    """

    role: str = Field(...)
    content: str = Field(...)

    def to_msg_tuple(self):
        return "ai" if self.role == "assistant" else "human", self.content

    @classmethod
    def from_data(cls, h: Union[List, Tuple, Dict]) -> "History":
        if isinstance(h, (list, tuple)) and len(h) >= 2:
            h = cls(role=h[0], content=h[1])
        elif isinstance(h, dict):
            h = cls(**h)

        return h


class MsgType:
    TEXT = 1
    IMAGE = 2
    AUDIO = 3
    VIDEO = 4


class BaseResponse(BaseModel):
    code: int = Field(200, description="API status code")
    msg: str = Field("success", description="API status message")
    data: Any = Field(None, description="API data")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": 200,
                "msg": "success",
            }
        }
    )


class ListResponse(BaseResponse):
    data: List[Any] = Field(..., description="List of data")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": 200,
                "msg": "success",
                "data": ["doc1.docx", "doc2.pdf", "doc3.txt"],
            }
        }
    )


class OpenAIBaseOutput(BaseModel):
    id: Optional[str] = None
    content: Optional[str] = None
    model: Optional[str] = None
    object: Literal[
        "chat.completion", "chat.completion.chunk"
    ] = "chat.completion.chunk"
    role: Literal["assistant"] = "assistant"
    finish_reason: Optional[str] = None
    created: int = Field(default_factory=lambda: int(time.time()))
    tool_calls: List[Dict] = []

    status: Optional[int] = None  # AgentStatus
    message_type: int = MsgType.TEXT
    message_id: Optional[str] = None  # id in database table
    is_ref: bool = False  # wheather show in seperated expander

    model_config = ConfigDict(extra="allow")

    def model_dump(self) -> dict:
        result = {
            "id": self.id,
            "object": self.object,
            "model": self.model,
            "created": self.created,
            "status": self.status,
            "message_type": self.message_type,
            "message_id": self.message_id,
            "is_ref": self.is_ref,
            "role": self.role,
        }
        if self.content is not None:
            result["content"] = self.content
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        if self.finish_reason is not None:
            result["finish_reason"] = self.finish_reason
        # Pydantic v2 extra 字段（docs / raw_docs / session_id 等）
        extras = getattr(self, "__pydantic_extra__", None) or {}
        for k, v in extras.items():
            if k not in result:
                result[k] = v
        # 兼容旧式 setattr 写入的字段
        for k, v in self.__dict__.items():
            if k not in result and not k.startswith("_"):
                result[k] = v
        # OpenAI SDK / WebUI 读 choices；顶层 content 保留给旧客户端
        if self.object == "chat.completion.chunk":
            delta: dict[str, Any] = {}
            if self.content is not None:
                delta["content"] = self.content
            if self.content in (None, "") and self.role:
                delta.setdefault("role", self.role)
            result["choices"] = [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": self.finish_reason,
                }
            ]
        elif self.object == "chat.completion" and self.content is not None:
            result["choices"] = [
                {
                    "index": 0,
                    "message": {"role": self.role, "content": self.content},
                    "finish_reason": self.finish_reason or "stop",
                }
            ]
        return result


class OpenAIChatOutput(OpenAIBaseOutput):
    model_config = ConfigDict(extra="allow")

    def dict(self, **kwargs) -> dict:
        return self.model_dump()

    def model_dump_json(self, **kwargs) -> str:
        kwargs.setdefault("ensure_ascii", False)
        return json.dumps(self.model_dump(), **kwargs)
