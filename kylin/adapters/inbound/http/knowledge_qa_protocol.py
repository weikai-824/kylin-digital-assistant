"""将知识问答应用事件映射为旧 HTTP/SSE 输出协议。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Optional
from urllib.parse import urlencode

from sse_starlette.sse import EventSourceResponse

from kylin.adapters.inbound.http.knowledge_qa_output import (
    chat_id,
    make_chat_output,
    sse_error,
)
from kylin.platform.runtime.net import api_address
from kylin.ports.application.knowledge_qa import (
    PROGRESS_STATUS_TEXT,
    KnowledgeQaApplicationPort,
    KnowledgeQaEvent,
    KnowledgeQaRequest,
)
from kylin.ports.model.gateway import ModelGatewayPort
from kylin.ports.data.knowledge_search import (
    KnowledgeBaseNotFoundError,
    KnowledgeSearchError,
)
from kylin.platform.runtime.schemas import BaseResponse, History
from kylin.platform.config.settings import Settings
from kylin.platform.observability.logging import build_logger

logger = build_logger()

# 单次问答的服务端总时长上限（秒）。
# 与前端 oa-console 的 STREAM_TIMEOUTS.total（120s）对齐：前端到点会主动 abort，
# 但服务端不一定感知得到（客户端断连不总能触发 CancelledError，实测断开后无中断日志），
# 没有上限就会继续占着 LLM / 检索资源空跑。留 5s 余量，让前端先给提示、服务端再收尾。
MAX_STREAM_SECONDS: float = 125.0


def _format_merged_reference(
    docs: Sequence[object], default_kb: str, api_base_url: str
) -> list[str]:
    """将应用文档转换为旧 API 的出处列表。"""
    source_documents: list[str] = []
    for index, document in enumerate(docs, start=1):
        metadata = dict(getattr(document, "metadata", None) or {})
        kb = metadata.get("_kb_name") or default_kb
        filename = metadata.get("source") or "未知来源"
        parameters = urlencode({"knowledge_base_name": kb, "file_name": filename})
        url = f"{api_base_url.strip(' /')}/knowledge_base/download_doc?{parameters}"
        content = getattr(document, "page_content", "")
        source_documents.append(f"出处 [{index}] [{filename}]({url}) \n\n{content}\n\n")
    return source_documents


def _raw_documents(docs: Sequence[object]) -> list[dict]:
    """透出前端证据面板需要的稳定字段，剔除 vector 大字段。"""
    return [
        {
            "page_content": getattr(document, "page_content", ""),
            "metadata": {
                key: value
                for key, value in dict(
                    getattr(document, "metadata", None) or {}
                ).items()
                if key != "vector"
            },
        }
        for document in docs
    ]


def _serialize_event(
    event: KnowledgeQaEvent,
    *,
    default_kb: str,
    api_url: str,
) -> str:
    docs = _format_merged_reference(event.documents, default_kb, api_url)
    raw_docs = _raw_documents(event.documents)
    if event.is_refusal and event.kind in ("start", "complete"):
        docs = ["<span style='color:red'>未找到相关文档，已拒绝无依据作答。</span>"]
    if event.kind == "progress":
        stage = event.content or ""
        return make_chat_output(
            session_id=event.session_id,
            id=chat_id(),
            object="chat.completion.chunk",
            content="",
            role="assistant",
            model=event.model,
            progress=stage,
            status_text=PROGRESS_STATUS_TEXT.get(stage, stage),
        ).model_dump_json()
    if event.kind == "thought":
        # 结构化思考中间产物：前端据此渲染虚化思维链（多行，随到随推）
        return make_chat_output(
            session_id=event.session_id,
            id=chat_id(),
            object="chat.completion.chunk",
            content="",
            role="assistant",
            model=event.model,
            event="thought",
            stage=event.stage,
            delta=event.content or "",
        ).model_dump_json()
    if event.kind == "start":
        return make_chat_output(
            session_id=event.session_id,
            id=chat_id(),
            object="chat.completion.chunk",
            content="",
            role="assistant",
            model=event.model,
            docs=docs,
            raw_docs=raw_docs,
            ticket_id=event.ticket_id,
        ).model_dump_json()
    if event.kind == "token":
        return make_chat_output(
            session_id=event.session_id,
            id=chat_id(),
            object="chat.completion.chunk",
            content=event.content,
            role="assistant",
            model=event.model,
        ).model_dump_json()
    # complete：非流式带正文用 chat.completion；流式收尾（content 已逐 token 发过）
    # 用 chunk + finish_reason，让 OpenAI 流式客户端立刻结束，不堵在连接上。
    if event.content:
        return make_chat_output(
            session_id=event.session_id,
            id=chat_id(),
            object="chat.completion",
            content=event.content,
            role="assistant",
            model=event.model,
            docs=docs,
            raw_docs=raw_docs,
            finish_reason=event.finish_reason,
            ticket_id=event.ticket_id,
        ).model_dump_json()
    return make_chat_output(
        session_id=event.session_id,
        id=chat_id(),
        object="chat.completion.chunk",
        content="",
        role="assistant",
        model=event.model,
        docs=docs,
        raw_docs=raw_docs,
        finish_reason=event.finish_reason or "stop",
        ticket_id=event.ticket_id,
    ).model_dump_json()


def _is_exempt(user_id: str | None, dept: str | None) -> bool:
    """判断用户是否豁免部门过滤（配置 EXEMPT_USERIDS / EXEMPT_DEPTS）。"""
    settings = Settings.basic_settings
    if user_id and user_id.strip() in (settings.EXEMPT_USERIDS or []):
        return True
    if dept and dept.strip() in (settings.EXEMPT_DEPTS or []):
        return True
    return False


class KnowledgeQaHttpProtocolAdapter:
    """把旧问答参数和输出协议映射到知识问答应用端口。"""

    def __init__(
        self,
        service: KnowledgeQaApplicationPort,
        model_gateway: ModelGatewayPort,
    ):
        self._service = service
        self._model_gateway = model_gateway

    async def chat(
        self,
        query: str,
        kb_name: str = "",
        top_k: int = None,
        score_threshold: float = None,
        history: list = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        dept: Optional[str] = None,
        stream: bool = True,
        model: str = None,
        temperature: float = None,
        max_tokens: Optional[int] = None,
        prompt_name: str = "default",
        return_direct: bool = False,
        **_ignored,
    ):
        """保持原有调用参数与输出协议的知识库问答入口。"""
        del prompt_name, _ignored
        top_k = Settings.kb_settings.VECTOR_SEARCH_TOP_K if top_k is None else top_k
        score_threshold = (
            Settings.kb_settings.SCORE_THRESHOLD
            if score_threshold is None
            else score_threshold
        )
        model = model or self._model_gateway.default_llm()
        temperature = (
            Settings.model_settings.TEMPERATURE
            if temperature is None
            else temperature
        )
        max_tokens = (
            Settings.model_settings.MAX_TOKENS if max_tokens is None else max_tokens
        )
        service = self._service
        if not service.knowledge_base_exists(kb_name):
            return BaseResponse(code=404, msg=f"未找到知识库 {kb_name}")

        started_at = time.monotonic()

        async def iterator():
            try:
                # 豁免用户/部门不受检索过滤限制（dept_exempt=True，检索层不过滤）
                request = KnowledgeQaRequest(
                    query=query,
                    knowledge_base_name=kb_name,
                    top_k=top_k,
                    score_threshold=score_threshold,
                    history=[History.from_data(item) for item in (history or [])],
                    session_id=session_id,
                    user_id=user_id,
                    dept=dept,
                    dept_exempt=_is_exempt(user_id, dept),
                    stream=stream,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    return_direct=return_direct,
                )
                api_url = api_address(is_public=True)
                async for event in service.run(request):
                    # 服务端兜底：整轮超时直接收尾，避免前端已放弃、后端还在空跑
                    if time.monotonic() - started_at > MAX_STREAM_SECONDS:
                        logger.warning(
                            "knowledge chat stream timeout after %.0fs kb=%s",
                            MAX_STREAM_SECONDS,
                            kb_name,
                        )
                        yield sse_error(
                            f"生成超时（{int(MAX_STREAM_SECONDS)}s），已停止生成，请简化问题后重试"
                        )
                        return
                    yield _serialize_event(event, default_kb=kb_name, api_url=api_url)
            except asyncio.CancelledError:
                logger.warning("streaming progress has been interrupted by user.")
            except KnowledgeBaseNotFoundError:
                yield sse_error(f"未找到知识库 {kb_name}")
            except KnowledgeSearchError as error:
                logger.error("检索后端故障 kb=%s: %s", kb_name, error)
                yield sse_error(
                    f"检索服务暂不可用（{error.backend} 后端故障），请稍后重试"
                )
            except Exception as error:
                logger.error("error in knowledge chat: %s", error)
                yield sse_error(error)

        if stream:
            return EventSourceResponse(iterator())
        return await iterator().__anext__()
