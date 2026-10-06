"""模型平台注册与 LLM / Embeddings / Rerank 客户端工厂（共享基础设施）。

当前供模型出站适配器和知识库数据适配器复用；具体模型访问集中在本模块，
业务层只通过模型端口使用。

注意：函数默认值一律延迟到调用时解析，避免 import 即发网络请求/读配置。
"""

from __future__ import annotations

from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    Union,
)
from urllib.parse import urlparse

import httpx
import openai
import requests
from memoization import cached, CachingAlgorithmFlag

from kylin.adapters.outbound.model.zhipuai import ZhipuAIEmbeddings
from kylin.platform.runtime.net import api_address, get_httpx_client
from kylin.platform.config.settings import Settings, XF_MODELS_TYPES
from kylin.platform.observability.logging import build_logger

logger = build_logger()

# vLLM / Qwen 推理模型：关闭 thinking 可让 content 直接是最终答案
DISABLE_THINKING_EXTRA_BODY: Dict[str, Any] = {
    "chat_template_kwargs": {"enable_thinking": False},
}


class LLMResponse:
    """兼容 langchain AIMessage 的最小响应对象（content 属性）。"""

    def __init__(self, content: str):
        self.content = content or ""


class ChatLLM:
    """轻量 Chat 客户端（openai SDK 直连），替代 langchain ChatOpenAI。

    - invoke(messages) -> LLMResponse（同步）
    - ainvoke(messages) -> LLMResponse（异步）
    - astream(messages) -> AsyncGenerator[str]（异步流式，原生 token 序列）
    messages 为 OpenAI 兼容 dict 列表 [{"role", "content"}, ...]。
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        proxy: Optional[str] = None,
        timeout: Optional[float] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra_body = dict(extra_body or {})
        client_kwargs: Dict[str, Any] = {}
        if base_url:
            client_kwargs["base_url"] = base_url
        client_kwargs["api_key"] = api_key or "EMPTY"
        client_kwargs["timeout"] = timeout or Settings.basic_settings.HTTPX_DEFAULT_TIMEOUT
        if proxy:
            client_kwargs["http_client"] = get_httpx_client(
                use_async=True, proxies=proxy
            )
        self._async = openai.AsyncClient(**client_kwargs)
        self._sync = openai.Client(**client_kwargs)

    def close(self) -> None:
        try:
            self._async.close()
        except Exception:
            pass
        try:
            self._sync.close()
        except Exception:
            pass

    def _completion_kwargs(self) -> Dict[str, Any]:
        kw: Dict[str, Any] = {"model": self.model}
        if self.temperature is not None:
            kw["temperature"] = self.temperature
        if self.max_tokens is not None:
            kw["max_tokens"] = self.max_tokens
        if self.extra_body:
            kw["extra_body"] = self.extra_body
        return kw

    def invoke(self, messages: List[Dict[str, str]]) -> LLMResponse:
        resp = self._sync.chat.completions.create(
            messages=messages, **self._completion_kwargs()
        )
        content = resp.choices[0].message.content if resp.choices else ""
        return LLMResponse(content)

    async def ainvoke(self, messages: List[Dict[str, str]]) -> LLMResponse:
        resp = await self._async.chat.completions.create(
            messages=messages, **self._completion_kwargs()
        )
        content = resp.choices[0].message.content if resp.choices else ""
        return LLMResponse(content)

    async def astream(self, messages: List[Dict[str, str]]):
        """原生异步流式：逐 token 产出（替代 AsyncIteratorCallbackHandler）。"""
        stream = await self._async.chat.completions.create(
            messages=messages,
            stream=True,
            **self._completion_kwargs(),
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            token = getattr(delta, "content", None)
            if token:
                yield token



def get_base_url(url):
    parsed_url = urlparse(url)  # 解析url
    base_url = '{uri.scheme}://{uri.netloc}/'.format(uri=parsed_url)  # 格式化基础url
    return base_url.rstrip('/')


def get_config_platforms() -> Dict[str, Dict]:
    """
    获取配置的模型平台，会将 pydantic model 转换为字典。
    """
    platforms = [m.model_dump() for m in Settings.model_settings.MODEL_PLATFORMS]
    return {m["platform_name"]: m for m in platforms}


@cached(max_size=10, ttl=60, algorithm=CachingAlgorithmFlag.LRU)


def detect_xf_models(xf_url: str) -> Dict[str, List[str]]:
    '''
    use cache for xinference model detecting to avoid:
    - too many requests in short intervals
    - multiple requests to one platform for every model
    the cache will be invalidated after one minute
    '''
    xf_model_type_maps = {
        "llm_models": lambda xf_models: [k for k, v in xf_models.items()
                                         if "LLM" == v["model_type"]
                                         and "vision" not in v["model_ability"]],
        "embed_models": lambda xf_models: [k for k, v in xf_models.items()
                                           if "embedding" == v["model_type"]],
        "text2image_models": lambda xf_models: [k for k, v in xf_models.items()
                                                if "image" == v["model_type"]],
        "image2image_models": lambda xf_models: [k for k, v in xf_models.items()
                                                 if "image" == v["model_type"]],
        "image2text_models": lambda xf_models: [k for k, v in xf_models.items()
                                                if "LLM" == v["model_type"]
                                                and "vision" in v["model_ability"]],
        "rerank_models": lambda xf_models: [k for k, v in xf_models.items()
                                            if "rerank" == v["model_type"]],
        "speech2text_models": lambda xf_models: [k for k, v in xf_models.items()
                                                 if v.get(list(XF_MODELS_TYPES["speech2text"].keys())[0])
                                                 in XF_MODELS_TYPES["speech2text"].values()],
        "text2speech_models": lambda xf_models: [k for k, v in xf_models.items()
                                                 if v.get(list(XF_MODELS_TYPES["text2speech"].keys())[0])
                                                 in XF_MODELS_TYPES["text2speech"].values()],
    }
    models = {}
    try:
        from xinference_client import RESTfulClient as Client
        xf_client = Client(xf_url)
        xf_models = xf_client.list_models()
        for m_type, filter in xf_model_type_maps.items():
            models[m_type] = filter(xf_models)
    except ImportError:
        logger.warning('auto_detect_model needs xinference-client installed. '
                       'Please try "pip install xinference-client". ')
    except requests.exceptions.ConnectionError:
        logger.warning(f"cannot connect to xinference host: {xf_url}, please check your configuration.")
    except Exception as e:
        logger.warning(f"error when connect to xinference server({xf_url}): {e}")
    return models


def get_config_models(
        model_name: str = None,
        model_type: Optional[Literal[
            "llm", "embed", "text2image", "image2image", "image2text", "rerank", "speech2text", "text2speech"
        ]] = None,
        platform_name: str = None,
) -> Dict[str, Dict]:
    """
    获取配置的模型列表，返回值为:
    {model_name: {
        "platform_name": xx,
        "platform_type": xx,
        "model_type": xx,
        "model_name": xx,
        "api_base_url": xx,
        "api_key": xx,
        "api_proxy": xx,
    }}
    """
    result = {}
    if model_type is None:
        model_types = [
            "llm_models",
            "embed_models",
            "text2image_models",
            "image2image_models",
            "image2text_models",
            "rerank_models",
            "speech2text_models",
            "text2speech_models",
        ]
    else:
        model_types = [f"{model_type}_models"]

    for m in list(get_config_platforms().values()):
        if platform_name is not None and platform_name != m.get("platform_name"):
            continue

        if m.get("auto_detect_model"):
            if not m.get("platform_type") == "xinference":  # TODO：当前仅支持 xf 自动检测模型
                logger.warning(f"auto_detect_model not supported for {m.get('platform_type')} yet")
                continue
            xf_url = get_base_url(m.get("api_base_url"))
            xf_models = detect_xf_models(xf_url)
            for m_type in model_types:
                # if m.get(m_type) != "auto":
                #     continue
                m[m_type] = xf_models.get(m_type, [])

        for m_type in model_types:
            models = m.get(m_type, [])
            if models == "auto":
                logger.warning("you should not set `auto` without auto_detect_model=True")
                continue
            elif not models:
                continue
            for m_name in models:
                if model_name is None or model_name == m_name:
                    result[m_name] = {
                        "platform_name": m.get("platform_name"),
                        "platform_type": m.get("platform_type"),
                        "model_type": m_type.split("_")[0],
                        "model_name": m_name,
                        "api_base_url": m.get("api_base_url"),
                        "api_key": m.get("api_key"),
                        "api_proxy": m.get("api_proxy"),
                        "disable_thinking": bool(m.get("disable_thinking", False)),
                        "grounding_style": m.get("grounding_style", "strict"),
                    }
    return result


def get_model_info(
        model_name: str = None, platform_name: str = None, multiple: bool = False
) -> Dict:
    """
    获取配置的模型信息，主要是 api_base_url, api_key
    如果指定 multiple=True，则返回所有重名模型；否则仅返回第一个
    """
    result = get_config_models(model_name=model_name, platform_name=platform_name)
    if len(result) > 0:
        if multiple:
            return result
        else:
            return list(result.values())[0]
    else:
        return {}


def get_default_llm():
    available_llms = list(get_config_models(model_type="llm").keys())
    if Settings.model_settings.DEFAULT_LLM_MODEL in available_llms:
        return Settings.model_settings.DEFAULT_LLM_MODEL
    else:
        logger.warning(f"default llm model {Settings.model_settings.DEFAULT_LLM_MODEL} is not found in available llms, "
                       f"using {available_llms[0]} instead")
        return available_llms[0]


def get_default_embedding():
    available_embeddings = list(get_config_models(model_type="embed").keys())
    if Settings.model_settings.DEFAULT_EMBEDDING_MODEL in available_embeddings:
        return Settings.model_settings.DEFAULT_EMBEDDING_MODEL
    else:
        logger.warning(f"default embedding model {Settings.model_settings.DEFAULT_EMBEDDING_MODEL} is not found in "
                       f"available embeddings, using {available_embeddings[0]} instead")
        return available_embeddings[0]


def get_default_rerank():
    available_reranks = list(get_config_models(model_type="rerank").keys())
    default_name = Settings.model_settings.DEFAULT_RERANK_MODEL
    if not available_reranks:
        logger.warning("no rerank model configured in MODEL_PLATFORMS")
        return default_name
    if default_name in available_reranks:
        return default_name
    logger.warning(
        f"default rerank model {default_name} is not found in available reranks, "
        f"using {available_reranks[0]} instead"
    )
    return available_reranks[0]


def get_ChatOpenAI(
        model_name: str = None,
        temperature: float = None,
        max_tokens: int = None,
        streaming: bool = True,
        callbacks: List[Callable] = [],
        verbose: bool = True,
        local_wrap: bool = False,  # use local wrapped api
        **kwargs: Any,
) -> ChatLLM:
    # 默认值延迟到调用时解析：避免 import 即触发网络探测/读配置
    model_name = model_name or get_default_llm()
    temperature = (
        Settings.model_settings.TEMPERATURE if temperature is None else temperature
    )
    max_tokens = (
        Settings.model_settings.MAX_TOKENS if max_tokens is None else max_tokens
    )
    model_info = get_model_info(model_name)
    base_url = f"{api_address()}/v1" if local_wrap else model_info.get("api_base_url")
    api_key = "EMPTY" if local_wrap else model_info.get("api_key")
    proxy = None if local_wrap else model_info.get("api_proxy")
    try:
        model = ChatLLM(
            model=model_name,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            proxy=proxy,
            extra_body=kwargs.pop("extra_body", None),
        )
    except Exception as e:
        logger.exception(f"failed to create ChatLLM for model: {model_name}.")
        model = None
    return model


def get_ChatPlatformAIParams(
        model_name: str = None,
        temperature: float = None,
        max_tokens: int = None,
        streaming: bool = True,
        callbacks: List[Callable] = [],
        verbose: bool = True,
        local_wrap: bool = False,  # use local wrapped api
        **kwargs: Any,
) -> Dict:
    model_info = get_model_info(model_name)
    if not model_info:
        raise ValueError(f"cannot find model info for model: {model_name}")

    params = dict(
        streaming=streaming,
        verbose=verbose,
        callbacks=callbacks,
        model=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
        **kwargs,
    )
    # remove paramters with None value to avoid openai validation error
    for k in list(params):
        if params[k] is None:
            params.pop(k)

    try:
        params.update(
            api_base=model_info.get("api_base_url"),
            api_key=model_info.get("api_key"),
            proxy=model_info.get("api_proxy"),
        )
        return params
    except Exception as e:
        logger.exception(f"failed to create for model: {model_name}.")
        return {}


def get_OpenAI(
        model_name: str,
        temperature: float,
        max_tokens: int = None,
        streaming: bool = True,
        echo: bool = True,
        callbacks: List[Callable] = [],
        verbose: bool = True,
        local_wrap: bool = False,  # use local wrapped api
        **kwargs: Any,
) -> ChatLLM:
    max_tokens = (
        Settings.model_settings.MAX_TOKENS if max_tokens is None else max_tokens
    )
    # TODO: 从API获取模型信息
    model_info = get_model_info(model_name)
    base_url = f"{api_address()}/v1" if local_wrap else model_info.get("api_base_url")
    api_key = "EMPTY" if local_wrap else model_info.get("api_key")
    proxy = None if local_wrap else model_info.get("api_proxy")
    try:
        model = ChatLLM(
            model=model_name,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            proxy=proxy,
        )
    except Exception as e:
        logger.exception(f"failed to create ChatLLM for model: {model_name}.")
        model = None
    return model


class OpenAIEmbeddingClient:
    """轻量 Embeddings 客户端（openai SDK 直连），替代 langchain OpenAIEmbeddings。

    - embed_query(text) -> List[float]
    - embed_documents(texts) -> List[List[float]]
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        proxy: Optional[str] = None,
    ):
        self.model = model
        client_kwargs: Dict[str, Any] = {}
        if base_url:
            client_kwargs["base_url"] = base_url
        client_kwargs["api_key"] = api_key or "EMPTY"
        if proxy:
            client_kwargs["http_client"] = get_httpx_client(proxies=proxy)
        self._client = openai.Client(**client_kwargs)

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        # 部分 Embedding 服务（如阿里 qwen3.7-text-embedding）限制单次 input 不超过 20 条，
        # 这里按批切分后合并，保持顺序与一次性调用一致。
        # encoding_format/dimensions：modelscope 等服务要求显式 encoding_format；
        # Qwen3-Embedding 等 MRL 模型用 dimensions 截到 EMBEDDING_DIM 以匹配 Milvus collection 维度。
        batch_size = 20
        out: List[List[float]] = []
        dim = Settings.kb_settings.EMBEDDING_DIM or None
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            kwargs = {"model": self.model, "input": batch, "encoding_format": "float"}
            if dim:
                kwargs["dimensions"] = dim
            resp = self._client.embeddings.create(**kwargs)
            data = sorted(resp.data, key=lambda d: d.index)
            out.extend(d.embedding for d in data)
        return out


class OllamaEmbeddingClient:
    """Ollama embeddings：优先 /v1/embeddings 批量，失败再逐条 /api/embeddings。"""

    def __init__(self, *, model: str, base_url: str):
        self.model = model
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = Settings.basic_settings.HTTPX_DEFAULT_TIMEOUT

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            resp = requests.post(
                f"{self.base_url}/v1/embeddings",
                json={"model": self.model, "input": list(texts)},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            rows = resp.json().get("data") or []
            rows = sorted(rows, key=lambda item: item["index"])
            if len(rows) != len(texts):
                raise RuntimeError(
                    f"Ollama /v1/embeddings 返回条数不匹配: {len(rows)} != {len(texts)}"
                )
            return [row["embedding"] for row in rows]
        except Exception as batch_error:
            logger.warning(
                f"Ollama 批量 embeddings 失败，回退逐条 /api/embeddings: {batch_error}"
            )
            out: List[List[float]] = []
            for text in texts:
                resp = requests.post(
                    f"{self.base_url}/api/embeddings",
                    json={"model": self.model, "prompt": text},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                out.append(resp.json()["embedding"])
            return out


def get_Embeddings(
        embed_model: str = None,
        local_wrap: bool = False,  # use local wrapped api
):
    embed_model = embed_model or get_default_embedding()
    model_info = get_model_info(model_name=embed_model)
    try:
        if local_wrap:
            return OpenAIEmbeddingClient(
                model=embed_model,
                base_url=f"{api_address()}/v1",
                api_key="EMPTY",
            )
        platform = model_info.get("platform_type")
        if platform == "ollama":
            return OllamaEmbeddingClient(
                model=embed_model,
                base_url=model_info.get("api_base_url", "").replace("/v1", ""),
            )
        if platform == "zhipuai":
            return ZhipuAIEmbeddings(
                base_url=model_info.get("api_base_url"),
                api_key=model_info.get("api_key"),
                zhipuai_proxy=model_info.get("api_proxy"),
                model=embed_model,
            )
        # openai / local（xinference 等 /v1 兼容）统一走 openai SDK
        return OpenAIEmbeddingClient(
            model=embed_model,
            base_url=model_info.get("api_base_url"),
            api_key=model_info.get("api_key"),
            proxy=model_info.get("api_proxy"),
        )
    except Exception as e:
        logger.exception(f"failed to create Embeddings for model: {embed_model}.")
        raise e


def check_embed_model(embed_model: str = None) -> Tuple[bool, str]:
    '''
    check weather embed_model accessable, use default embed model if None
    '''
    embed_model = embed_model or get_default_embedding()
    embeddings = get_Embeddings(embed_model=embed_model)
    try:
        embeddings.embed_query("this is a test")
        return True, ""
    except Exception as e:
        msg = f"failed to access embed model '{embed_model}': {e}"
        logger.error(msg)
        return False, msg


def get_OpenAIClient(
        platform_name: str = None,
        model_name: str = None,
        is_async: bool = True,
) -> Union[openai.Client, openai.AsyncClient]:
    """
    construct an openai Client for specified platform or model
    """
    if platform_name is None:
        platform_info = get_model_info(
            model_name=model_name, platform_name=platform_name
        )
        if platform_info is None:
            raise RuntimeError(
                f"cannot find configured platform for model: {model_name}"
            )
        platform_name = platform_info.get("platform_name")
    platform_info = get_config_platforms().get(platform_name)
    assert platform_info, f"cannot find configured platform: {platform_name}"
    params = {
        "base_url": platform_info.get("api_base_url"),
        "api_key": platform_info.get("api_key"),
    }
    httpx_params = {}
    if api_proxy := platform_info.get("api_proxy"):
        httpx_params = {
            "proxies": api_proxy,
            "transport": httpx.HTTPTransport(local_address="0.0.0.0"),
        }

    if is_async:
        if httpx_params:
            params["http_client"] = httpx.AsyncClient(**httpx_params)
        return openai.AsyncClient(**params)
    else:
        if httpx_params:
            params["http_client"] = httpx.Client(**httpx_params)
        return openai.Client(**params)
