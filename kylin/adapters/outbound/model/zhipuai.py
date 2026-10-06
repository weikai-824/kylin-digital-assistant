"""智谱 AI Embeddings 客户端（精简实现，openai SDK 直连）。

P7 决策：移除 langchain 全家。原实现为 vendored langchain_glm 风格
（依赖 langchain_core.pydantic_v1），智谱平台提供 OpenAI 兼容端点，
直接用 openai SDK 即可，保留 embed_query / embed_documents 接口。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import openai

from kylin.platform.runtime.net import get_httpx_client


class ZhipuAIEmbeddings:
    """ZhipuAI embedding models（OpenAI 兼容端点直连）。

    构造参数与旧实现对齐：base_url / api_key / zhipuai_proxy / model。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        zhipuai_proxy: Optional[str] = None,
        model: str = "embedding-2",
        **kwargs: Any,
    ):
        self.model = model
        client_kwargs: Dict[str, Any] = {}
        if base_url:
            client_kwargs["base_url"] = base_url
        if api_key:
            client_kwargs["api_key"] = api_key
        if zhipuai_proxy:
            client_kwargs["http_client"] = get_httpx_client(proxies=zhipuai_proxy)
        self._client = openai.Client(**client_kwargs)

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        resp = self._client.embeddings.create(model=self.model, input=texts)
        data = sorted(resp.data, key=lambda d: d.index)
        return [d.embedding for d in data]
