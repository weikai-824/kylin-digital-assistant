"""对话路由：知识库问答、提示词改写与 OpenAI 兼容补全。"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Request

from kylin.adapters.inbound.http.knowledge_qa import KnowledgeQaHttpAdapter
from kylin.adapters.inbound.http.openai import openai_request
from kylin.adapters.inbound.http.schemas import OpenAIChatInput
from kylin.adapters.outbound.model.chat import KylinChatModelAdapter
from kylin.applications.knowledge_qa.prompt_optimizer import PromptOptimizer
from kylin.platform.config.settings import Settings
from kylin.platform.security.authorization import require_permission
from kylin.ports.model.gateway import ModelGatewayPort


def create_chat_router(
    knowledge_qa: KnowledgeQaHttpAdapter,
    model_gateway: ModelGatewayPort,
    prompt_optimizer: PromptOptimizer | None = None,
) -> APIRouter:
    """创建保持原有 `/chat` 路径的路由。"""
    router = APIRouter(
        prefix="/chat",
        tags=["Kylin 对话"],
        dependencies=[Depends(require_permission("ai:digital-human:chat"))],
    )
    router.post("/kb_chat", summary="知识库对话")(knowledge_qa.chat)

    # 发送前的提示词改写旁路：独立于 kb_chat 主链路，一次 LLM 调用改写后回填输入框。
    # 不检索、不写会话；optimizer 内部任何失败都回退原文（notes 以 fallback_/exception: 开头），
    # HTTP 仍 200，由前端根据 notes 决定是否提示。
    @router.post("/optimize_prompt", summary="优化提示词（改写输入，不触发检索）")
    def optimize_prompt(
        query: str = Body(
            ...,
            embed=True,
            description="用户原始输入",
            examples=["怎么登录这个"],
        ),
    ) -> Dict[str, Any]:
        """把口语/省略的问句改写为完整清晰的问句，仅改写不检索."""
        optimizer = prompt_optimizer or PromptOptimizer(KylinChatModelAdapter())
        result = optimizer.optimize(query)
        return {
            "code": 200,
            "msg": result.notes or "ok",
            "data": {
                "optimized": result.text,
                "kind": result.kind,
                "notes": result.notes,
            },
        }

    @router.post("/chat/completions", summary="兼容 openai 的统一 chat 接口")
    async def chat_completions(
        request: Request,
        body: OpenAIChatInput,
    ) -> Dict:
        """转发为普通 LLM 对话。"""
        del request
        if body.max_tokens in [None, 0]:
            body.max_tokens = Settings.model_settings.MAX_TOKENS

        extra = {**body.model_extra} or {}
        for key in list(extra):
            delattr(body, key)

        client = model_gateway.openai_client(model_name=body.model, is_async=True)
        return await openai_request(client.chat.completions.create, body, extra)

    return router
