"""为 FastAPI 文档页挂载仓库内的离线静态资源。"""

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.staticfiles import StaticFiles
from starlette.responses import HTMLResponse


def make_fastapi_offline(
    app: FastAPI,
    static_dir: Path = Path(__file__).parent / "static",
    static_url: str = "/static-offline-docs",
    docs_url: Optional[str] = "/docs",
    redoc_url: Optional[str] = "/redoc",
) -> None:
    """将 FastAPI 的 Swagger 和 ReDoc 页面切换到本地静态资源。"""
    openapi_url = app.openapi_url
    swagger_redirect_url = app.swagger_ui_oauth2_redirect_url

    def remove_route(url: Optional[str]) -> None:
        if url is None:
            return
        for index, route in enumerate(app.routes):
            if route.path.lower() == url.lower():
                app.routes.pop(index)
                return

    app.mount(
        static_url,
        StaticFiles(directory=static_dir.as_posix()),
        name="static-offline-docs",
    )

    if docs_url is not None:
        remove_route(docs_url)
        remove_route(swagger_redirect_url)

        @app.get(docs_url, include_in_schema=False)
        async def custom_swagger_ui_html(request: Request) -> HTMLResponse:
            root = request.scope.get("root_path") or ""
            return get_swagger_ui_html(
                openapi_url=f"{root}{openapi_url}",
                title=app.title + " - Swagger UI",
                oauth2_redirect_url=swagger_redirect_url,
                swagger_js_url=f"{root}{static_url}/swagger-ui-bundle.js",
                swagger_css_url=f"{root}{static_url}/swagger-ui.css",
                swagger_favicon_url=f"{root}{static_url}/favicon.png",
            )

        if swagger_redirect_url is not None:

            @app.get(swagger_redirect_url, include_in_schema=False)
            async def swagger_ui_redirect() -> HTMLResponse:
                return get_swagger_ui_oauth2_redirect_html()

    if redoc_url is not None:
        remove_route(redoc_url)

        @app.get(redoc_url, include_in_schema=False)
        async def redoc_html(request: Request) -> HTMLResponse:
            root = request.scope.get("root_path") or ""
            return get_redoc_html(
                openapi_url=f"{root}{openapi_url}",
                title=app.title + " - ReDoc",
                redoc_js_url=f"{root}{static_url}/redoc.standalone.js",
                with_google_fonts=False,
                redoc_favicon_url=f"{root}{static_url}/favicon.png",
            )
