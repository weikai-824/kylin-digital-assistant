"""重排序：调用远程 /v1/rerank 对检索结果重排（失败则回退原序）。"""

from __future__ import annotations

from typing import Dict, List, Optional

from kylin.platform.observability.logging import build_logger

logger = build_logger()


def rerank_document_text(doc: Dict) -> str:
    """拼接文档来源/章节与子块正文，供 Cross-Encoder 打分。"""
    body = (doc.get("page_content") or "").strip() if isinstance(doc, dict) else str(doc).strip()
    meta = dict(doc.get("metadata") or {}) if isinstance(doc, dict) else {}
    source = (meta.get("source") or meta.get("doc_title") or "").strip()
    title_path = (meta.get("title_path") or "").strip()
    section = (meta.get("section_title") or "").strip()
    chapter = title_path or section
    header_parts: list[str] = []
    if source:
        header_parts.append(f"【文档】{source}")
    if chapter:
        header_parts.append(f"【章节】{chapter}")
    if not header_parts:
        return body
    return "\n".join(header_parts) + ("\n" + body if body else "")


def rerank_documents(
    query: str,
    docs: List[Dict],
    top_n: Optional[int] = None,
    model_name: Optional[str] = None,
) -> List[Dict]:
    """
    通过 MODEL_PLATFORMS 中配置的 Rerank HTTP 服务重排文档。
    docs 为 search_docs 返回的 dict 列表；失败时原样回退。
    """
    if not docs:
        return []

    from kylin.adapters.outbound.model.clients import get_default_rerank, get_model_info
    from kylin.platform.runtime.net import get_httpx_client

    model_name = model_name or get_default_rerank()
    top_n = top_n if top_n is not None else len(docs)
    top_n = min(top_n, len(docs))

    model_info = get_model_info(model_name)
    if not model_info:
        logger.warning(f"rerank model '{model_name}' not found in MODEL_PLATFORMS, skip rerank")
        return docs[:top_n]

    api_base_url = (model_info.get("api_base_url") or "").rstrip("/")
    if not api_base_url:
        logger.warning(f"rerank model '{model_name}' missing api_base_url, skip rerank")
        return docs[:top_n]

    url = f"{api_base_url}/rerank"
    documents_text = [rerank_document_text(d) for d in docs]
    payload = {
        "model": model_name,
        "query": query,
        "documents": documents_text,
        "top_n": top_n,
    }
    headers = {"Content-Type": "application/json"}
    api_key = model_info.get("api_key")
    if api_key and api_key not in ("EMPTY", "dummy"):
        headers["Authorization"] = f"Bearer {api_key}"

    # 内网 rerank 地址不要走系统代理（否则 httpx 可能被代理劫持或初始化失败）
    unused = []
    try:
        from urllib.parse import urlparse

        parsed = urlparse(api_base_url)
        if parsed.hostname:
            if parsed.port:
                unused = [f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"]
            else:
                unused = [f"{parsed.scheme}://{parsed.hostname}"]
    except Exception:
        unused = []

    try:
        with get_httpx_client(use_async=False, unused_proxies=unused) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error(f"rerank request failed ({url}): {e}")
        return docs[:top_n]

    results = data.get("results") or []
    if not results:
        logger.warning(f"rerank returned empty results: {data}")
        return docs[:top_n]

    reranked: List[Dict] = []
    for item in results:
        idx = item.get("index")
        if idx is None or not (0 <= idx < len(docs)):
            continue
        doc = dict(docs[idx])
        metadata = dict(doc.get("metadata") or {})
        metadata["relevance_score"] = item.get("relevance_score")
        doc["metadata"] = metadata
        if "score" in doc or "score" in docs[idx]:
            doc["score"] = item.get("relevance_score", doc.get("score"))
        reranked.append(doc)

    return reranked[:top_n] if reranked else docs[:top_n]
