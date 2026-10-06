"""知识问答 FastAPI 参数与会话历史适配器。"""

from __future__ import annotations

from typing import List, Optional

from fastapi import Body, Request

from kylin.adapters.inbound.http.knowledge_qa_protocol import (
    KnowledgeQaHttpProtocolAdapter,
)
from kylin.ports.data.conversation_memory import ConversationMemoryPort
from kylin.platform.runtime.schemas import History


class KnowledgeQaHttpAdapter:
    """把 FastAPI 请求参数映射到问答协议和会话记忆端口。"""

    def __init__(
        self,
        protocol: KnowledgeQaHttpProtocolAdapter,
        memory: ConversationMemoryPort,
    ):
        self._protocol = protocol
        self._memory = memory

    async def get_session_history(
        self,
        session_id: str = Body("", description="会话ID（sess_xxx）"),
    ):
        """查询会话历史；不存在时保持旧接口的空消息响应。"""
        if not session_id:
            return {
                "code": 400,
                "msg": "session_id 不能为空",
                "session_id": "",
                "kb_name": "",
                "summary": "",
                "messages": [],
            }
        archive = self._memory.find(session_id)
        if archive is None:
            return {
                "code": 404,
                "msg": "会话不存在或已过期",
                "session_id": session_id,
                "kb_name": "",
                "summary": "",
                "messages": [],
            }
        return {
            "code": 200,
            "session_id": archive.session_id,
            "kb_name": archive.knowledge_base_name,
            "summary": archive.summary,
            "messages": list(archive.messages),
        }

    async def chat(
        self,
        query: str = Body(..., description="用户输入", examples=["你好"]),
        kb_name: str = Body("", description="知识库名称", examples=["oa_kb"]),
        top_k: int = Body(None, description="返回文档数；缺省由检索层解析默认"),
        score_threshold: float = Body(
            None,
            description="向量距离阈值，SCORE越小越严，取2不筛",
            ge=0,
            le=2,
        ),
        history: List[History] = Body([], description="历史对话"),
        session_id: Optional[str] = Body(
            None,
            description="会话ID（sess_xxx）；无则服务端新建并在首个响应中返回",
        ),
        user_id: Optional[str] = Body(
            None,
            description="用户身份ID（OA userid）；用于长期记忆按人隔离，可为空",
        ),
        dept: Optional[str] = Body(
            None,
            description="用户所属部门 ID（OA departmentId）；用于检索按部门过滤，为空则不过滤",
        ),
        stream: bool = Body(True, description="流式输出"),
        model: str = Body(None, description="LLM 模型名称；缺省由服务端解析默认"),
        temperature: float = Body(
            None,
            description="LLM 采样温度；缺省由服务端解析默认",
            ge=0.0,
            le=2.0,
        ),
        max_tokens: Optional[int] = Body(
            None,
            description="限制LLM生成Token数量；缺省由服务端解析默认",
        ),
        prompt_name: str = Body("default", description="prompt 模板名"),
        return_direct: bool = Body(False, description="直接返回检索结果，不送入 LLM"),
        request: Request = None,
        mode: Optional[str] = Body(None, description="已废弃，忽略"),
    ):
        """OA / WebUI 知识库问答 API（仅双索引知识库）。"""
        del mode
        return await self._protocol.chat(
            query=query,
            kb_name=kb_name,
            top_k=top_k,
            score_threshold=score_threshold,
            history=history,
            session_id=session_id,
            user_id=user_id,
            dept=dept,
            stream=stream,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_name=prompt_name,
            return_direct=return_direct,
            request=request,
        )
