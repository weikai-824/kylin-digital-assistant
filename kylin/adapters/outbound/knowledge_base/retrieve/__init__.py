"""检索出站能力的兼容导出。

重型检索客户端按需加载，使纯 RRF 逻辑和架构测试不必初始化外部 SDK。
"""

from importlib import import_module

from kylin.adapters.outbound.knowledge_base.retrieve.rrf import rrf_fuse

_LAZY_EXPORTS = {
    "dual_search": ("dual_search", "dual_search"),
    "dual_search_trace": ("dual_search", "dual_search_trace"),
    "search_es_children": ("dual_search", "search_es_children"),
    "search_milvus_children": ("dual_search", "search_milvus_children"),
    "rerank_documents": ("rerank", "rerank_documents"),
}


def __getattr__(name: str):
    """首次访问兼容导出时再加载对应的远程检索实现。"""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute_name = target
    value = getattr(
        import_module(
            f"kylin.adapters.outbound.knowledge_base.retrieve.{module_name}"
        ),
        attribute_name,
    )
    globals()[name] = value
    return value


__all__ = [
    "dual_search",
    "dual_search_trace",
    "rerank_documents",
    "rrf_fuse",
    "search_es_children",
    "search_milvus_children",
]
