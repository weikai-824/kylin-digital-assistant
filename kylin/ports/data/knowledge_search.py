"""知识问答所需的知识库检索契约."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from collections.abc import Mapping
from typing import Any, Protocol, Sequence


class KnowledgeBaseNotFoundError(LookupError):
    """指定知识库不存在."""


class KnowledgeBaseUnavailableError(RuntimeError):
    """知识库依赖的模型或后端当前不可用."""


class KnowledgeSearchError(RuntimeError):
    """检索后端查询失败，不能按空结果处理."""

    def __init__(self, backend: str, knowledge_base_name: str, detail: str = ""):
        self.backend = backend
        self.knowledge_base_name = knowledge_base_name
        self.detail = detail
        message = f"检索后端 {backend} 故障"
        if knowledge_base_name:
            message += f"（知识库 {knowledge_base_name}）"
        if detail:
            message += f": {detail}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    """应用层使用的知识文档，不包含存储 SDK 类型."""

    page_content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    document_id: str = ""
    score: float = 3.0

    def with_metadata(self, **values: Any) -> "KnowledgeDocument":
        """返回合并元数据后的新文档."""
        return replace(self, metadata={**self.metadata, **values})

    def with_score(self, score: float) -> "KnowledgeDocument":
        """返回同步更新顶层分数和元数据分数的新文档."""
        metadata = {**self.metadata, "score": score, "rrf_score": score}
        return replace(self, score=score, metadata=metadata)


class KnowledgeSearchPort(Protocol):
    """单知识库、单查询检索能力."""

    def exists(self, knowledge_base_name: str) -> bool:
        """返回知识库是否存在."""

    def ensure_ready(self, knowledge_base_name: str) -> None:
        """确认知识库及其检索模型可用，否则抛出领域异常."""

    def search(
        self,
        query: str,
        *,
        knowledge_base_name: str,
        top_k: int,
        score_threshold: float,
        file_name: str = "",
        metadata: Mapping[str, Any] | None = None,
        dept: str | None = None,
        dept_exempt: bool = False,
    ) -> Sequence[KnowledgeDocument]:
        """返回按相关性排序或按筛选条件列出的知识文档.

        dept: 用户所属部门 ID（OA departmentId）。
        dept_exempt: 是否豁免部门过滤（系统内部调用/配置豁免用户或部门）。
        过滤语义见 dual_search.build_department_filter：
        - 豁免 → 不过滤（全部可见）
        - 匿名（dept 为空）→ 仅未打标（公开）文档
        - 部门用户 → 公开文档 + 本部门文档
        """
