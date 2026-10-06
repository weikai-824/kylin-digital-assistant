"""描述是否检索以及如何检索的结构化计划."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 并行子问上限：多实体查询（"分别/和/以及"并列多个系统）每实体拆一子问，
# 2 上限会导致子问被整句 standalone 挤占（实测 046/048 费控/远程安全评估子问丢失）。
# 4 足够覆盖常见多实体，且 ThreadPoolExecutor 并行 4 worker，延迟影响有限。
DEFAULT_SUB_QUERY_MAX = 4


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """一次用户提问对应的检索计划."""

    standalone_query: str
    need_retrieval: bool = True
    query_type: str = "factual"
    sub_queries: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def normalized_sub_queries(
        self,
        *,
        max_n: int = DEFAULT_SUB_QUERY_MAX,
    ) -> list[str]:
        """去空、去重并限制并行检索数量；始终保留 standalone.

        优先保留计划子问顺序（概览类第 1 项应为主题核心名词），再把
        ``standalone_query`` 并入集合：未满 ``max_n`` 则追加；已满则替换最后一项，
        避免挤掉首个核心短语。
        """
        if max_n <= 0:
            raise ValueError("max_n must be greater than zero")

        standalone = (self.standalone_query or "").strip()
        seen: set[str] = set()
        others: list[str] = []
        for query in self.sub_queries or []:
            query = (query or "").strip()
            if not query or query == standalone or query in seen:
                continue
            seen.add(query)
            others.append(query)

        if not standalone:
            return others[:max_n]
        if not others:
            return [standalone]
        if standalone in others:
            return others[:max_n]
        if len(others) < max_n:
            return [*others, standalone][:max_n]
        # 已满：保住 others[0]（核心词），末位换成 standalone
        return [*others[: max_n - 1], standalone]
