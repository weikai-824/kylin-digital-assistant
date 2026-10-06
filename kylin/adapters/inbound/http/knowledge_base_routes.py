"""知识库管理与兼容问答路由。"""

from __future__ import annotations

from typing import List, Literal

from fastapi import APIRouter, Depends, Request

from kylin.adapters.inbound.http.knowledge_base import KnowledgeBaseHttpAdapter
from kylin.adapters.inbound.http.knowledge_qa import KnowledgeQaHttpAdapter
from kylin.adapters.inbound.http.knowledge_search import KnowledgeSearchHttpAdapter
from kylin.adapters.inbound.http.schemas import OpenAIChatInput
from kylin.ports.application.knowledge_ingestion import KnowledgeIngestionPort
from kylin.ports.data.knowledge_search import KnowledgeSearchPort
from kylin.platform.runtime.schemas import BaseResponse, ListResponse
from kylin.platform.config.settings import Settings
from kylin.platform.security.authorization import require_permission


def create_knowledge_base_router(
    ingestion: KnowledgeIngestionPort,
    knowledge_qa: KnowledgeQaHttpAdapter,
    search_port: KnowledgeSearchPort,
) -> APIRouter:
    """创建保持原有 `/knowledge_base` 路径的路由。"""
    router = APIRouter(
        prefix="/knowledge_base",
        tags=["Knowledge Base Management"],
    )
    knowledge_base = KnowledgeBaseHttpAdapter(ingestion)
    knowledge_search = KnowledgeSearchHttpAdapter(search_port)

    @router.post(
        "/{mode}/{param}/chat/completions",
        summary="知识库对话（openai 兼容）；mode 仅支持 local_kb",
        dependencies=[Depends(require_permission("ai:digital-human:kb:query"))],
    )
    async def kb_chat_endpoint(
        mode: Literal["local_kb"],
        param: str,
        body: OpenAIChatInput,
        request: Request,
    ):
        del mode
        if body.max_tokens in [None, 0]:
            body.max_tokens = Settings.model_settings.MAX_TOKENS

        extra = body.model_extra or {}
        return await knowledge_qa.chat(
            query=body.messages[-1]["content"],
            kb_name=param,
            top_k=extra.get("top_k", Settings.kb_settings.VECTOR_SEARCH_TOP_K),
            score_threshold=extra.get(
                "score_threshold", Settings.kb_settings.SCORE_THRESHOLD
            ),
            history=body.messages[:-1],
            session_id=extra.get("session_id"),
            user_id=extra.get("user_id"),
            dept=extra.get("dept"),
            stream=body.stream,
            model=body.model,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            prompt_name=extra.get("prompt_name", "default"),
            return_direct=extra.get("return_direct", False),
            request=request,
        )

    @router.post(
        "/session_history",
        summary="查询会话历史（刷新后恢复界面用）",
        dependencies=[Depends(require_permission("ai:digital-human:kb:query"))],
    )
    async def session_history_endpoint(body: dict = None):
        """会话历史只读查询。body: {"session_id": "sess_xxx"}"""
        payload = body or {}
        return await knowledge_qa.get_session_history(
            session_id=payload.get("session_id", "")
        )

    router.get(
        "/list_knowledge_bases",
        response_model=ListResponse,
        summary="获取知识库列表",
        dependencies=[Depends(require_permission("ai:digital-human:kb:query"))],
    )(knowledge_base.list_kbs)
    router.post(
        "/create_knowledge_base",
        response_model=BaseResponse,
        summary="创建知识库",
        dependencies=[Depends(require_permission("ai:digital-human:kb:create"))],
    )(knowledge_base.create_kb)
    router.post(
        "/delete_knowledge_base",
        response_model=BaseResponse,
        summary="删除知识库",
        dependencies=[Depends(require_permission("ai:digital-human:kb:delete"))],
    )(knowledge_base.delete_kb)
    router.get(
        "/list_files",
        response_model=ListResponse,
        summary="获取知识库内的文件列表",
        dependencies=[Depends(require_permission("ai:digital-human:kb:query"))],
    )(
        knowledge_base.list_files
    )
    router.post(
        "/search_docs",
        response_model=List[dict],
        summary="搜索知识库",
        dependencies=[Depends(require_permission("ai:digital-human:kb:query"))],
    )(
        knowledge_search.search_docs
    )
    router.post(
        "/upload_docs",
        response_model=BaseResponse,
        summary="上传文件到知识库并向量化",
        dependencies=[Depends(require_permission("ai:digital-human:kb:upload"))],
    )(
        knowledge_base.upload_docs
    )
    router.post(
        "/delete_docs",
        response_model=BaseResponse,
        summary="删除知识库内指定文件",
        dependencies=[Depends(require_permission("ai:digital-human:kb:delete"))],
    )(
        knowledge_base.delete_docs
    )
    router.post(
        "/update_info",
        response_model=BaseResponse,
        summary="更新知识库介绍",
        dependencies=[Depends(require_permission("ai:digital-human:kb:update"))],
    )(
        knowledge_base.update_info
    )
    router.post(
        "/update_docs",
        response_model=BaseResponse,
        summary="更新现有文件到知识库",
        dependencies=[Depends(require_permission("ai:digital-human:kb:update"))],
    )(
        knowledge_base.update_docs
    )
    router.post(
        "/update_access_depts",
        response_model=BaseResponse,
        summary="更新文件可见范围（只刷权限，不重建内容；托管库可用）",
        dependencies=[Depends(require_permission("ai:digital-human:kb:update"))],
    )(knowledge_base.update_access_depts)
    router.get(
        "/download_doc",
        response_model=None,
        summary="下载知识文件",
        dependencies=[Depends(require_permission("ai:digital-human:kb:query"))],
    )(
        knowledge_base.download_doc
    )
    router.post(
        "/recreate_vector_store",
        summary="根据 content 重建索引，流式输出进度",
        dependencies=[Depends(require_permission("ai:digital-human:kb:rebuild"))],
    )(
        knowledge_base.rebuild
    )
    return router
