"""系统托管知识库：OA 同步库与工单反哺库，禁止 WebUI/普通 API 变更。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence


class ManagedKnowledgeBaseError(PermissionError):
    """试图对 OA / 工单等系统托管知识库做用户侧变更."""

    def __init__(self, name: str, *, action: str = "修改"):
        self.name = name
        self.action = action
        super().__init__(
            f"知识库「{name}」由系统托管（OA 同步 / 工单反哺），可查看不可改，禁止通过 WebUI 或普通接口{action}。"
            f"请使用独立业务知识库（如 oa_manuals）上传与维护。"
        )


def normalize_kb_names(names: Iterable[str | None]) -> frozenset[str]:
    """去空、去重后的知识库名集合."""
    return frozenset(str(name).strip() for name in names if name and str(name).strip())


def resolve_managed_knowledge_bases(
    *,
    ticket_kb_name: str = "",
    oa_kb_name: str = "",
    extra: Sequence[str] | None = None,
) -> frozenset[str]:
    """汇总当前环境下的系统托管库名."""
    return normalize_kb_names(
        (
            ticket_kb_name,
            oa_kb_name,
            *(extra or ()),
        )
    )


class ManagedKnowledgeBaseGuard:
    """入库服务使用的托管库写保护."""

    def __init__(self, names: Sequence[str] | Callable[[], Iterable[str]] = ()):
        if callable(names):
            self._provider: Callable[[], Iterable[str]] = names
            self._static: frozenset[str] | None = None
        else:
            self._provider = lambda: ()
            self._static = normalize_kb_names(names)

    def names(self) -> frozenset[str]:
        if self._static is not None:
            return self._static
        return normalize_kb_names(self._provider())

    def is_managed(self, name: str) -> bool:
        normalized = (name or "").strip()
        return bool(normalized) and normalized in self.names()

    def assert_mutable(self, name: str, *, allow_system_managed: bool, action: str) -> None:
        if allow_system_managed:
            return
        normalized = (name or "").strip()
        if self.is_managed(normalized):
            raise ManagedKnowledgeBaseError(normalized, action=action)
