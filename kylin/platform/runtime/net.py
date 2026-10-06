"""网络地址与 HTTP 客户端工厂（共享基础设施）。

当前供 HTTP、WebUI 与模型出站适配器复用。
"""

from __future__ import annotations

import os
from typing import Dict, List, Union

import httpx

from kylin.platform.config.settings import Settings

def api_address(is_public: bool = False) -> str:
    '''
    允许用户在 basic_settings.API_SERVER 中配置 public_host, public_port
    以便使用云服务器或反向代理时生成正确的公网 API 地址（如知识库文档下载链接）
    '''
    from kylin.platform.config.settings import Settings

    server = Settings.basic_settings.API_SERVER
    if is_public:
        host = server.get("public_host", "127.0.0.1")
        port = server.get("public_port", "7861")
    else:
        host = server.get("host", "127.0.0.1")
        port = server.get("port", "7861")
        if host == "0.0.0.0":
            host = "127.0.0.1"
    return f"http://{host}:{port}"


def webui_address() -> str:
    from kylin.platform.config.settings import Settings

    host = Settings.basic_settings.WEBUI_SERVER["host"]
    port = Settings.basic_settings.WEBUI_SERVER["port"]
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def set_httpx_config(
        timeout: float = None,
        proxy: Union[str, Dict] = None,
        unused_proxies: List[str] = [],
):
    """
    设置httpx默认timeout。httpx默认timeout是5秒，在请求LLM回答时不够用。
    将本项目相关服务加入无代理列表，避免fastchat的服务器请求错误。(windows下无效)
    对于chatgpt等在线API，如要使用代理需要手动配置。搜索引擎的代理如何处置还需考虑。
    """
    # 默认值延迟到调用时解析：避免 import 即读配置
    from kylin.platform.config.settings import Settings

    if timeout is None:
        timeout = Settings.basic_settings.HTTPX_DEFAULT_TIMEOUT

    import os

    import httpx

    httpx._config.DEFAULT_TIMEOUT_CONFIG.connect = timeout
    httpx._config.DEFAULT_TIMEOUT_CONFIG.read = timeout
    httpx._config.DEFAULT_TIMEOUT_CONFIG.write = timeout

    # 在进程范围内设置系统级代理
    proxies = {}
    if isinstance(proxy, str):
        for n in ["http", "https", "all"]:
            proxies[n + "_proxy"] = proxy
    elif isinstance(proxy, dict):
        for n in ["http", "https", "all"]:
            if p := proxy.get(n):
                proxies[n + "_proxy"] = p
            elif p := proxy.get(n + "_proxy"):
                proxies[n + "_proxy"] = p

    for k, v in proxies.items():
        os.environ[k] = v

    # set host to bypass proxy
    no_proxy = [
        x.strip() for x in os.environ.get("no_proxy", "").split(",") if x.strip()
    ]
    no_proxy += [
        # do not use proxy for locahost
        "http://127.0.0.1",
        "http://localhost",
    ]
    # do not use proxy for user deployed fastchat servers
    for x in unused_proxies:
        host = ":".join(x.split(":")[:2])
        if host not in no_proxy:
            no_proxy.append(host)
    os.environ["NO_PROXY"] = ",".join(no_proxy)

    def _get_proxies():
        return proxies

    import urllib.request

    urllib.request.getproxies = _get_proxies


def get_httpx_client(
        use_async: bool = False,
        proxies: Union[str, Dict] = None,
        timeout: float = None,
        unused_proxies: List[str] = [],
        **kwargs,
) -> Union[httpx.Client, httpx.AsyncClient]:
    """
    helper to get httpx client with default proxies that bypass local addesses.
    """
    # 默认值延迟到调用时解析：避免 import 即读配置
    from kylin.platform.config.settings import Settings as _S
    if timeout is None:
        timeout = _S.basic_settings.HTTPX_DEFAULT_TIMEOUT
    default_proxies = {
        # do not use proxy for locahost
        "all://127.0.0.1": None,
        "all://localhost": None,
    }
    # do not use proxy for user deployed fastchat servers
    for x in unused_proxies:
        host = ":".join(x.split(":")[:2])
        default_proxies.update({host: None})
        # httpx 0.28 mounts 更认 all://host 形式
        try:
            from urllib.parse import urlparse

            parsed = urlparse(x if "://" in x else f"http://{x}")
            if parsed.hostname:
                default_proxies[f"all://{parsed.hostname}"] = None
        except Exception:
            pass

    # get proxies from system envionrent
    # proxy not str empty string, None, False, 0, [] or {}
    default_proxies.update(
        {
            "http://": (
                os.environ.get("http_proxy")
                if os.environ.get("http_proxy")
                   and len(os.environ.get("http_proxy").strip())
                else None
            ),
            "https://": (
                os.environ.get("https_proxy")
                if os.environ.get("https_proxy")
                   and len(os.environ.get("https_proxy").strip())
                else None
            ),
            "all://": (
                os.environ.get("all_proxy")
                if os.environ.get("all_proxy")
                   and len(os.environ.get("all_proxy").strip())
                else None
            ),
        }
    )
    for host in os.environ.get("no_proxy", "").split(","):
        if host := host.strip():
            # default_proxies.update({host: None}) # Origin code
            default_proxies.update(
                {"all://" + host: None}
            )  # PR 1838 fix, if not add 'all://', httpx will raise error

    # merge default proxies with user provided proxies
    if isinstance(proxies, str):
        proxies = {"all://": proxies}

    if isinstance(proxies, dict):
        default_proxies.update(proxies)

    # construct Client（兼容 httpx<0.28 的 proxies 与 0.28+ 的 mounts/proxy）
    import inspect

    client_cls = httpx.AsyncClient if use_async else httpx.Client
    init_params = inspect.signature(client_cls.__init__).parameters
    client_kwargs = dict(kwargs)
    client_kwargs["timeout"] = timeout

    if "proxies" in init_params:
        client_kwargs["proxies"] = default_proxies
    elif "mounts" in init_params:
        transport_cls = httpx.AsyncHTTPTransport if use_async else httpx.HTTPTransport
        mounts = {}
        for pattern, proxy_url in default_proxies.items():
            mounts[pattern] = (
                transport_cls(proxy=proxy_url) if proxy_url else transport_cls()
            )
        client_kwargs["mounts"] = mounts
    elif "proxy" in init_params:
        proxy_url = (
            default_proxies.get("all://")
            or default_proxies.get("https://")
            or default_proxies.get("http://")
        )
        if proxy_url:
            client_kwargs["proxy"] = proxy_url

    return client_cls(**client_kwargs)
