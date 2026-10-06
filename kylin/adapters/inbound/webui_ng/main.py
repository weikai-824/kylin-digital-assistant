"""NiceGUI 调试台组合根：FastAPI 工厂 + ui.run_with 挂载。

由 startup.py 的 run_webui() 以 uvicorn 拉起：
- 开发模式：``uvicorn.run("...main:create_webui_app", factory=True, reload=True)``
- 常规模式：create_webui_app() 后直接 uvicorn.run(app)

就绪信号：沿用 startup.py 现成的 _set_app_event（替换 lifespan_context），
与 API 子进程同一套路，启动语义（-w/--all 等待就绪）不变。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def create_webui_app():
    """创建挂载了 NiceGUI 页面的 FastAPI 应用（每次调用返回新实例）。"""
    # 兼容补丁必须在导入 settings / marshmallow 消费者之前生效
    from kylin.platform.runtime.compat_patches import apply_compat_patches

    apply_compat_patches()

    from fastapi import FastAPI
    from nicegui import ui

    from kylin import __version__
    from kylin.adapters.inbound.webui_ng import kb_page, tickets_page

    tickets_page.register_pages()  # 注册 @ui.page("/") 知识缺口工单主入口
    kb_page.register_pages()  # 注册 @ui.page("/kb") 知识库管理 / 文件上传

    webapp = FastAPI(title="Kylin Debug Console", docs_url=None, redoc_url=None)

    # NiceGUI 挂载到既有 FastAPI：不自带 reload（reload 由 uvicorn 负责），
    # 不用 ui.run()，由 startup.py 的 uvicorn 托管，端口/日志/就绪信号全部沿用现有约定。
    ui.run_with(
        webapp,
        title=f"系统咨询数字助手 · 调试台 v{__version__}",
        mount_path="/",
    )
    logger.info("NiceGUI 调试台已挂载到 FastAPI（mount_path=/）")
    return webapp
