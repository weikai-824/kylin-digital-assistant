"""FastAPI 组合根：注入应用服务并注册所有入站路由。"""

import argparse
from pathlib import Path

# 直接 python -m / run_api 入口也可能绕过 startup.run_api_server，此处再兜一次。
from kylin.platform.runtime.compat_patches import apply_compat_patches

apply_compat_patches()

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import RedirectResponse

from kylin import __version__
from kylin.adapters.inbound.http.chat import create_chat_router
from kylin.adapters.inbound.http.auth import create_auth_router
from kylin.adapters.inbound.http.completion import create_completion_handler
from kylin.adapters.inbound.http.identity import create_identity_router
from kylin.adapters.inbound.http.knowledge_base_routes import (
    create_knowledge_base_router,
)
from kylin.adapters.inbound.http.knowledge_gap import create_ticket_router
from kylin.adapters.inbound.http.knowledge_qa import KnowledgeQaHttpAdapter
from kylin.adapters.inbound.http.knowledge_qa_protocol import (
    KnowledgeQaHttpProtocolAdapter,
)
from kylin.adapters.inbound.http.offline_docs import make_fastapi_offline
from kylin.adapters.inbound.http.openai import openai_router
from kylin.adapters.inbound.http.server_state import server_router
from kylin.adapters.outbound.data.conversation_memory import (
    KylinConversationMemoryAdapter,
)
from kylin.adapters.outbound.data.knowledge_search import KylinKnowledgeSearchAdapter
from kylin.adapters.outbound.model.chat import KylinChatModelAdapter
from kylin.adapters.outbound.model.gateway import KylinModelGatewayAdapter
from kylin.adapters.outbound.observability import build_trace_port
from kylin.applications.knowledge_qa.prompt_optimizer import PromptOptimizer
from kylin.bootstrap.knowledge_gap import build_knowledge_gap_service
from kylin.bootstrap.knowledge_ingestion import build_knowledge_ingestion_service
from kylin.bootstrap.knowledge_qa import build_knowledge_qa_service
from kylin.platform.config.settings import Settings
from kylin.platform.security.middleware import create_auth_middleware


def create_app(run_mode: str = None):
    """创建 FastAPI 应用：挂载 chat/kb/openai/server 路由。"""
    app = FastAPI(title="系统咨询数字助手 API", version=__version__)
    model_gateway = KylinModelGatewayAdapter()
    app.state.model_gateway = model_gateway
    knowledge_gap = build_knowledge_gap_service()
    memory = KylinConversationMemoryAdapter()
    knowledge_search = KylinKnowledgeSearchAdapter()
    knowledge_qa_service = build_knowledge_qa_service(
        knowledge_gap=knowledge_gap,
        search_port=knowledge_search,
        memory=memory,
    )
    knowledge_qa = KnowledgeQaHttpAdapter(
        KnowledgeQaHttpProtocolAdapter(knowledge_qa_service, model_gateway),
        memory,
    )
    # 发送前的提示词改写旁路（独立于 kb_chat 主链路；同款 chat adapter，失败回退原文）
    prompt_optimizer = PromptOptimizer(KylinChatModelAdapter(trace=build_trace_port()))
    ingestion = build_knowledge_ingestion_service()
    make_fastapi_offline(app)
    # Add CORS middleware to allow all origins
    # 在config.py中设置OPEN_DOMAIN=True，允许跨域
    # set OPEN_DOMAIN=True in config.py to allow cross-domain
    if Settings.basic_settings.OPEN_CROSS_DOMAIN:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # P0-SEC-02：业务 API 全量鉴权（api_token 留空 = 不启用，行为不变）
    middleware_cls, middleware_kwargs = create_auth_middleware(
        Settings.basic_settings.api_token
    )
    app.add_middleware(middleware_cls, **middleware_kwargs)

    @app.get("/", summary="swagger 文档", include_in_schema=False)
    async def document():
        return RedirectResponse(url="/docs")

    @app.get("/health", summary="服务健康检查", include_in_schema=False)
    async def health():
        return {"status": "UP", "service": "digital-human"}

    app.include_router(
        create_chat_router(knowledge_qa, model_gateway, prompt_optimizer)
    )
    app.include_router(create_auth_router())
    app.include_router(
        create_knowledge_base_router(ingestion, knowledge_qa, knowledge_search)
    )
    app.include_router(openai_router)
    app.include_router(server_router)
    app.include_router(create_ticket_router(knowledge_gap))
    app.include_router(create_identity_router())

    # 其它接口
    app.post(
        "/other/completion",
        tags=["Other"],
        summary="要求llm模型补全(通过LLMChain)",
    )(create_completion_handler(model_gateway))

    # 媒体文件 / 项目图片：目录不存在时自动创建，避免 StaticFiles 启动崩溃
    media_dir = Path(Settings.basic_settings.MEDIA_PATH)
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")

    img_dir = Path(Settings.basic_settings.IMG_DIR)
    img_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/img", StaticFiles(directory=str(img_dir)), name="img")

    return app

def run_api(host, port, **kwargs):
    api_app = create_app()
    if kwargs.get("ssl_keyfile") and kwargs.get("ssl_certfile"):
        uvicorn.run(
            api_app,
            host=host,
            port=port,
            ssl_keyfile=kwargs.get("ssl_keyfile"),
            ssl_certfile=kwargs.get("ssl_certfile"),
        )
    else:
        uvicorn.run(api_app, host=host, port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="langchain-ChatGLM",
        description="About langchain-ChatGLM, local knowledge based ChatGLM with langchain"
        " ｜ 基于本地知识库的 ChatGLM 问答",
    )
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7861)
    parser.add_argument("--ssl_keyfile", type=str)
    parser.add_argument("--ssl_certfile", type=str)
    # 初始化消息
    args = parser.parse_args()
    args_dict = vars(args)

    run_api(
        host=args.host,
        port=args.port,
        ssl_keyfile=args.ssl_keyfile,
        ssl_certfile=args.ssl_certfile,
    )
