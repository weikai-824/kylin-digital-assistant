# 该文件封装了对 api.py 的请求，可以被不同的 webui 使用。
# P3 重构：统一 _request 重试模板、单份 SSE 解析规则；删除仅调试用的 AsyncApiRequest。

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

import httpx

from kylin.adapters.outbound.model.clients import get_default_embedding
from kylin.platform.runtime.net import api_address, get_httpx_client, set_httpx_config
from kylin.platform.config.settings import Settings
from kylin.platform.observability.logging import build_logger

__all__ = [
    "ApiRequest",
    "check_error_msg",
    "check_success_msg",
    "get_img_base64",
]

logger = build_logger()

set_httpx_config()


def _parse_sse_chunk(chunk: str, chunk_cache: List[str]) -> Optional[Any]:
    """解析单个 SSE chunk（规则单份，async/sync 共用）。

    - 空 chunk / SSE comment 行（`:` 开头）→ 跳过（不缓存）
    - `data: ` 前缀去掉前缀与末尾 2 字符；JSON 解析失败则拼入跨块缓存
    - 返回解析出的数据；None 表示跳过或需继续缓存
    """
    if not chunk:
        return None
    if chunk.startswith(":"):
        return None
    cache = chunk_cache[0]
    body = chunk[6:-2] if chunk.startswith("data: ") else chunk
    try:
        data = json.loads(cache + body)
        chunk_cache[0] = ""
        return data
    except Exception:
        chunk_cache[0] = cache + body
        return None


def _iter_sse_text(response, as_json: bool = False):
    """同步：把 httpx.stream 的 GeneratorContextManager 转成普通生成器。"""
    chunk_cache: List[str] = [""]
    try:
        with response as r:
            for chunk in r.iter_text(None):
                if not chunk:
                    continue
                if as_json:
                    data = _parse_sse_chunk(chunk, chunk_cache)
                    if data is not None:
                        yield data
                else:
                    yield chunk
    except httpx.ConnectError as e:
        msg = f"无法连接API服务器，请确认 ‘api.py’ 已正常启动。({e})"
        logger.error(msg)
        yield {"code": 500, "msg": msg}
    except httpx.ReadTimeout as e:
        msg = f"API通信超时，请确认已启动FastChat与API服务（详见Wiki '5. 启动 API 服务或 Web UI'）。（{e}）"
        logger.error(msg)
        yield {"code": 500, "msg": msg}
    except Exception as e:
        msg = f"API通信遇到错误：{e}"
        logger.error(f"{e.__class__.__name__}: {msg}")
        yield {"code": 500, "msg": msg}


async def _aiter_sse_text(response, as_json: bool = False):
    """异步：等价 _iter_sse_text 的 async 版本。"""
    chunk_cache: List[str] = [""]
    try:
        async with response as r:
            async for chunk in r.aiter_text(None):
                if not chunk:
                    continue
                if as_json:
                    data = _parse_sse_chunk(chunk, chunk_cache)
                    if data is not None:
                        yield data
                else:
                    yield chunk
    except httpx.ConnectError as e:
        msg = f"无法连接API服务器，请确认 ‘api.py’ 已正常启动。({e})"
        logger.error(msg)
        yield {"code": 500, "msg": msg}
    except httpx.ReadTimeout as e:
        msg = f"API通信超时，请确认已启动FastChat与API服务（详见Wiki '5. 启动 API 服务或 Web UI'）。（{e}）"
        logger.error(msg)
        yield {"code": 500, "msg": msg}
    except Exception as e:
        msg = f"API通信遇到错误：{e}"
        logger.error(f"{e.__class__.__name__}: {msg}")
        yield {"code": 500, "msg": msg}


class ApiRequest:
    """
    api.py调用的封装（同步模式），简化api调用方式
    """

    def __init__(
        self,
        base_url: str = None,
        timeout: float = None,
    ):
        # 默认值延迟到调用时解析：避免 import 即读配置
        if base_url is None:
            base_url = api_address()
        if timeout is None:
            timeout = Settings.basic_settings.HTTPX_DEFAULT_TIMEOUT
        self.base_url = base_url
        self.timeout = timeout
        self._use_async = False
        self._client = None

    @property
    def client(self):
        if self._client is None or self._client.is_closed:
            self._client = get_httpx_client(
                base_url=self.base_url, use_async=self._use_async, timeout=self.timeout
            )
        return self._client

    def _request(
        self,
        method: str,
        url: str,
        *,
        params=None,
        data=None,
        json=None,
        retry: int = 3,
        stream: bool = False,
        **kwargs: Any,
    ) -> Union[httpx.Response, Iterator[httpx.Response], None]:
        """统一请求模板：重试 + stream/非 stream（get/post/delete/put 共用）。"""
        # P0-SEC-02：API 配置了 token 时 WebUI 透传，避免全 API 鉴权后自身被 401
        api_token = (Settings.basic_settings.api_token or "").strip()
        if api_token:
            merged_headers = dict(kwargs.get("headers") or {})
            merged_headers.setdefault("Authorization", f"Bearer {api_token}")
            kwargs["headers"] = merged_headers
        while retry > 0:
            try:
                if stream:
                    return self.client.stream(
                        method, url, params=params, data=data, json=json, **kwargs
                    )
                return self.client.request(
                    method, url, params=params, data=data, json=json, **kwargs
                )
            except Exception as e:
                msg = f"error when {method.lower()} {url}: {e}"
                logger.error(f"{e.__class__.__name__}: {msg}")
                retry -= 1

    def get(
        self,
        url: str,
        params: Union[Dict, List[Tuple], bytes] = None,
        retry: int = 3,
        stream: bool = False,
        **kwargs: Any,
    ) -> Union[httpx.Response, Iterator[httpx.Response], None]:
        return self._request(
            "GET", url, params=params, retry=retry, stream=stream, **kwargs
        )

    def post(
        self,
        url: str,
        data: Dict = None,
        json: Dict = None,
        retry: int = 3,
        stream: bool = False,
        **kwargs: Any,
    ) -> Union[httpx.Response, Iterator[httpx.Response], None]:
        return self._request(
            "POST", url, data=data, json=json, retry=retry, stream=stream, **kwargs
        )

    def delete(
        self,
        url: str,
        data: Dict = None,
        json: Dict = None,
        retry: int = 3,
        stream: bool = False,
        **kwargs: Any,
    ) -> Union[httpx.Response, Iterator[httpx.Response], None]:
        return self._request(
            "DELETE", url, data=data, json=json, retry=retry, stream=stream, **kwargs
        )

    def put(
        self,
        url: str,
        data: Dict = None,
        json: Dict = None,
        retry: int = 3,
        stream: bool = False,
        **kwargs: Any,
    ) -> Union[httpx.Response, Iterator[httpx.Response], None]:
        return self._request(
            "PUT", url, data=data, json=json, retry=retry, stream=stream, **kwargs
        )

    def _httpx_stream2generator(
        self,
        response: contextlib._GeneratorContextManager,
        as_json: bool = False,
    ):
        """
        将httpx.stream返回的GeneratorContextManager转化为普通生成器
        """
        if self._use_async:
            return _aiter_sse_text(response, as_json)
        return _iter_sse_text(response, as_json)

    def _get_response_value(
        self,
        response: httpx.Response,
        as_json: bool = False,
        value_func: Callable = None,
    ):
        """
        转换同步或异步请求返回的响应
        `as_json`: 返回json
        `value_func`: 用户可以自定义返回值，该函数接受response或json
        """

        def to_json(r):
            try:
                return r.json()
            except Exception as e:
                msg = "API未能返回正确的JSON。" + str(e)
                logger.error(f"{e.__class__.__name__}: {msg}")
                return {"code": 500, "msg": msg, "data": None}

        if value_func is None:
            value_func = lambda r: r  # noqa: E731

        async def ret_async(response):
            if as_json:
                return value_func(to_json(await response))
            else:
                return value_func(await response)

        if self._use_async:
            return ret_async(response)
        else:
            if as_json:
                return value_func(to_json(response))
            else:
                return value_func(response)

    # 服务器信息
    def get_server_configs(self, **kwargs) -> Dict:
        response = self.post("/server/configs", **kwargs)
        return self._get_response_value(response, as_json=True)

    def get_prompt_template(
        self,
        type: str = "llm_chat",
        name: str = "default",
        **kwargs,
    ) -> str:
        data = {
            "type": type,
            "name": name,
        }
        response = self.post("/server/get_prompt_template", json=data, **kwargs)
        return self._get_response_value(response, value_func=lambda r: r.text)

    # 对话相关操作
    def chat_chat(
        self,
        query: str,
        metadata: dict,
        conversation_id: str = None,
        history_len: int = -1,
        history: List[Dict] = [],
        stream: bool = True,
        chat_model_config: Dict = None,
        tool_config: Dict = None,
        **kwargs,
    ):
        """
        对应api.py/chat/chat接口
        """
        data = {
            "query": query,
            "metadata": metadata,
            "conversation_id": conversation_id,
            "history_len": history_len,
            "history": history,
            "stream": stream,
            "chat_model_config": chat_model_config,
            "tool_config": tool_config,
        }

        response = self.post("/chat/chat", json=data, stream=True, **kwargs)
        return self._httpx_stream2generator(response, as_json=True)

    # 知识库相关操作

    def get_session_history(self, session_id: str) -> Dict:
        """
        查询会话历史（刷新后恢复界面用）
        """
        data = {"session_id": session_id}
        response = self.post("/knowledge_base/session_history", json=data)
        return self._get_response_value(response, as_json=True)

    def list_knowledge_bases(
        self,
    ):
        """
        对应api.py/knowledge_base/list_knowledge_bases接口
        """
        response = self.get("/knowledge_base/list_knowledge_bases")
        return self._get_response_value(
            response, as_json=True, value_func=lambda r: r.get("data", [])
        )

    def create_knowledge_base(
        self,
        knowledge_base_name: str,
        vector_store_type: str = None,
        embed_model: str = None,
    ):
        """
        对应api.py/knowledge_base/create_knowledge_base接口
        """
        # 默认值延迟到调用时解析：避免 import 即读配置/触发模型探测
        if vector_store_type is None:
            vector_store_type = Settings.kb_settings.DEFAULT_VS_TYPE
        if embed_model is None:
            embed_model = get_default_embedding()
        data = {
            "knowledge_base_name": knowledge_base_name,
            "vector_store_type": vector_store_type,
            "embed_model": embed_model,
        }

        response = self.post(
            "/knowledge_base/create_knowledge_base",
            json=data,
        )
        return self._get_response_value(response, as_json=True)

    def delete_knowledge_base(
        self,
        knowledge_base_name: str,
    ):
        """
        对应api.py/knowledge_base/delete_knowledge_base接口
        """
        response = self.post(
            "/knowledge_base/delete_knowledge_base",
            json=f"{knowledge_base_name}",
        )
        return self._get_response_value(response, as_json=True)

    def list_kb_docs(
        self,
        knowledge_base_name: str,
    ):
        """
        对应api.py/knowledge_base/list_files接口
        """
        response = self.get(
            "/knowledge_base/list_files",
            params={"knowledge_base_name": knowledge_base_name},
        )
        return self._get_response_value(
            response, as_json=True, value_func=lambda r: r.get("data", [])
        )

    def download_kb_doc(self, knowledge_base_name: str, file_name: str) -> bytes:
        """通过知识库 HTTP 接口读取源文件内容。"""
        response = self.get(
            "/knowledge_base/download_doc",
            params={
                "knowledge_base_name": knowledge_base_name,
                "file_name": file_name,
            },
        )
        if response is None:
            return b""
        response.raise_for_status()
        return response.content

    def search_kb_docs(
        self,
        knowledge_base_name: str,
        query: str = "",
        top_k: int = None,
        score_threshold: int = None,
        file_name: str = "",
        metadata: dict = {},
    ) -> List:
        """
        对应api.py/knowledge_base/search_docs接口
        """
        # 默认值延迟到调用时解析：避免 import 即读配置
        if top_k is None:
            top_k = Settings.kb_settings.VECTOR_SEARCH_TOP_K
        if score_threshold is None:
            score_threshold = Settings.kb_settings.SCORE_THRESHOLD
        data = {
            "query": query,
            "knowledge_base_name": knowledge_base_name,
            "top_k": top_k,
            "score_threshold": score_threshold,
            "file_name": file_name,
            "metadata": metadata,
        }

        response = self.post(
            "/knowledge_base/search_docs",
            json=data,
        )
        return self._get_response_value(response, as_json=True)

    def upload_kb_docs(
        self,
        files: List[Union[str, Path, bytes]],
        knowledge_base_name: str,
        override: bool = False,
        to_vector_store: bool = True,
        chunk_size=None,
        chunk_overlap=None,
        docs: Dict = {},
        metadata: Dict = {},
        not_refresh_vs_cache: bool = False,
    ):
        """
        对应api.py/knowledge_base/upload_docs接口
        """
        if chunk_size is None:
            chunk_size = Settings.kb_settings.CHILD_CHUNK_SIZE
        if chunk_overlap is None:
            chunk_overlap = Settings.kb_settings.CHILD_OVERLAP_SIZE

        def convert_file(file, filename=None):
            if isinstance(file, bytes):  # raw bytes
                file = BytesIO(file)
            elif hasattr(file, "read"):  # a file io like object
                filename = filename or file.name
            else:  # a local path
                file = Path(file).absolute().open("rb")
                filename = filename or os.path.split(file.name)[-1]
            return filename, file

        files = [convert_file(file) for file in files]
        data = {
            "knowledge_base_name": knowledge_base_name,
            "override": override,
            "to_vector_store": to_vector_store,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "docs": docs,
            "metadata": metadata,
            "not_refresh_vs_cache": not_refresh_vs_cache,
        }

        if isinstance(data["docs"], dict):
            data["docs"] = json.dumps(data["docs"], ensure_ascii=False)
        if isinstance(data["metadata"], dict):
            data["metadata"] = json.dumps(data["metadata"], ensure_ascii=False)
        response = self.post(
            "/knowledge_base/upload_docs",
            data=data,
            files=[("files", (filename, file)) for filename, file in files],
            # 解析+向量化经常超过默认 300s；失败勿盲重试，避免重复打满 CPU。
            timeout=Settings.basic_settings.HTTPX_KB_INGEST_TIMEOUT,
            retry=1,
        )
        return self._get_response_value(response, as_json=True)

    def delete_kb_docs(
        self,
        knowledge_base_name: str,
        file_names: List[str],
        delete_content: bool = False,
        not_refresh_vs_cache: bool = False,
    ):
        """
        对应api.py/knowledge_base/delete_docs接口
        """
        data = {
            "knowledge_base_name": knowledge_base_name,
            "file_names": file_names,
            "delete_content": delete_content,
            "not_refresh_vs_cache": not_refresh_vs_cache,
        }

        response = self.post(
            "/knowledge_base/delete_docs",
            json=data,
        )
        return self._get_response_value(response, as_json=True)

    def update_kb_info(self, knowledge_base_name, kb_info):
        """
        对应api.py/knowledge_base/update_info接口
        """
        data = {
            "knowledge_base_name": knowledge_base_name,
            "kb_info": kb_info,
        }

        response = self.post(
            "/knowledge_base/update_info",
            json=data,
        )
        return self._get_response_value(response, as_json=True)

    def update_kb_docs(
        self,
        knowledge_base_name: str,
        file_names: List[str],
        chunk_size=None,
        chunk_overlap=None,
        docs: Dict = {},
        access_depts: List[str] | None = None,
        not_refresh_vs_cache: bool = False,
    ):
        """
        对应api.py/knowledge_base/update_docs接口
        """
        if chunk_size is None:
            chunk_size = Settings.kb_settings.CHILD_CHUNK_SIZE
        if chunk_overlap is None:
            chunk_overlap = Settings.kb_settings.CHILD_OVERLAP_SIZE
        data = {
            "knowledge_base_name": knowledge_base_name,
            "file_names": file_names,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "docs": docs,
            "not_refresh_vs_cache": not_refresh_vs_cache,
        }
        if access_depts is not None:
            data["access_depts"] = access_depts

        if isinstance(data["docs"], dict):
            data["docs"] = json.dumps(data["docs"], ensure_ascii=False)

        response = self.post(
            "/knowledge_base/update_docs",
            json=data,
            timeout=Settings.basic_settings.HTTPX_KB_INGEST_TIMEOUT,
            retry=1,
        )
        return self._get_response_value(response, as_json=True)

    def update_doc_access_depts(
        self,
        knowledge_base_name: str,
        file_names: List[str],
        access_depts: List[str] = [],
    ):
        """
        对应api.py/knowledge_base/update_access_depts接口（只刷权限，不重建）。
        """
        data = {
            "knowledge_base_name": knowledge_base_name,
            "file_names": file_names,
            "access_depts": access_depts,
        }
        response = self.post(
            "/knowledge_base/update_access_depts",
            json=data,
        )
        return self._get_response_value(response, as_json=True)

    def recreate_vector_store(
        self,
        knowledge_base_name: str,
        allow_empty_kb: bool = True,
        vs_type: str = None,
        embed_model: str = None,
        chunk_size=None,
        chunk_overlap=None,
    ):
        """
        对应api.py/knowledge_base/recreate_vector_store接口
        """
        if vs_type is None:
            vs_type = Settings.kb_settings.DEFAULT_VS_TYPE
        if embed_model is None:
            embed_model = get_default_embedding()
        if chunk_size is None:
            chunk_size = Settings.kb_settings.CHILD_CHUNK_SIZE
        if chunk_overlap is None:
            chunk_overlap = Settings.kb_settings.CHILD_OVERLAP_SIZE
        data = {
            "knowledge_base_name": knowledge_base_name,
            "allow_empty_kb": allow_empty_kb,
            "vs_type": vs_type,
            "embed_model": embed_model,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
        }

        response = self.post(
            "/knowledge_base/recreate_vector_store",
            json=data,
            stream=True,
            timeout=None,
        )
        return self._httpx_stream2generator(response, as_json=True)

    def embed_texts(
        self,
        texts: List[str],
        embed_model: str = None,
        to_query: bool = False,
    ) -> List[List[float]]:
        """
        对文本进行向量化，可选模型包括本地 embed_models 和支持 embeddings 的在线模型
        """
        if embed_model is None:
            embed_model = get_default_embedding()
        data = {
            "texts": texts,
            "embed_model": embed_model,
            "to_query": to_query,
        }
        resp = self.post(
            "/other/embed_texts",
            json=data,
        )
        return self._get_response_value(
            resp, as_json=True, value_func=lambda r: r.get("data")
        )


def check_error_msg(data: Union[str, dict, list], key: str = "errorMsg") -> str:
    """
    return error message if error occured when requests API
    """
    if isinstance(data, dict):
        if key in data:
            return data[key]
        if "code" in data and data["code"] != 200:
            return data["msg"]
    return ""


def check_success_msg(data: Union[str, dict, list], key: str = "msg") -> str:
    """
    return error message if error occured when requests API
    """
    if (
        isinstance(data, dict)
        and key in data
        and "code" in data
        and data["code"] == 200
    ):
        return data[key]
    return ""


def get_img_base64(file_name: str) -> str:
    """
    get_img_base64 used in streamlit.
    absolute local path not working on windows.
    文件缺失时返回空串，避免整页崩溃。
    """
    image = f"{Settings.basic_settings.IMG_DIR}/{file_name}"
    try:
        with open(image, "rb") as f:
            buffer = BytesIO(f.read())
            base_str = base64.b64encode(buffer.getvalue()).decode()
        return f"data:image/png;base64,{base_str}"
    except OSError:
        logger.warning("图片缺失: %s", image)
        return ""
