"""文档切分出站能力的兼容导出。"""

from importlib import import_module

from .parent_child import ChildChunk, ParentChunk, split_markdown_parent_child

_LAZY_EXPORTS = {
    "split_text_to_documents": "split_text_to_documents",
    "split_text_to_parent_child_docs": "split_text_to_parent_child_docs",
}


def __getattr__(name: str):
    """按需加载依赖配置与解析器的文档转换入口。"""
    attribute_name = _LAZY_EXPORTS.get(name)
    if attribute_name is None:
        raise AttributeError(name)
    value = getattr(
        import_module("kylin.adapters.outbound.knowledge_base.split.to_documents"),
        attribute_name,
    )
    globals()[name] = value
    return value


__all__ = [
    "ChildChunk",
    "ParentChunk",
    "split_markdown_parent_child",
    "split_text_to_parent_child_docs",
    "split_text_to_documents",
]
