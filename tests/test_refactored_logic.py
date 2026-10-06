"""重构后核心纯逻辑的行为回归测试，不访问外部服务。"""

from __future__ import annotations

import unittest

from kylin.adapters.outbound.knowledge_base.retrieve.rrf import rrf_fuse
from kylin.adapters.outbound.knowledge_base.split.parent_child import (
    TITLE_SEP,
    split_markdown_parent_child,
)
from kylin.applications.knowledge_qa.grounding import sanitize_citations
from kylin.applications.knowledge_qa.query_plan.planner import (
    QueryPlanner,
    _coerce_plan,
    _extract_json,
    _fallback,
    _history_text,
)


class _FakeChatModel:
    def __init__(self, response: str | None):
        self.response = response

    def complete(self, messages, **kwargs):
        return self.response


class RefactoredLogicTests(unittest.TestCase):
    def test_query_plan_fallback_and_json_extraction(self) -> None:
        fallback = _fallback("原始问题", notes="reason")
        self.assertTrue(fallback.need_retrieval)
        self.assertEqual(["原始问题"], fallback.sub_queries)
        self.assertEqual(
            {"standalone_query": "年假"},
            _extract_json('```json\n{"standalone_query":"年假"}\n```'),
        )
        self.assertIsNone(_extract_json("不是 JSON"))

    def test_query_plan_coercion_normalizes_subqueries(self) -> None:
        plan = _coerce_plan(
            {
                "need_retrieval": False,
                "standalone_query": " 问题 ",
                "sub_queries": [" 子问 ", "子问", "", "第三个", "第四个"],
            },
            raw_query="原问",
        )
        self.assertFalse(plan.need_retrieval)
        # 保留计划子问顺序，上限 DEFAULT_SUB_QUERY_MAX=4；未满则追加 standalone
        self.assertEqual(["子问", "第三个", "第四个", "问题"], plan.sub_queries)

    def test_normalized_sub_queries_keeps_core_phrase_and_standalone(self) -> None:
        from kylin.applications.knowledge_qa.query_plan.schema import QueryPlan

        plan = QueryPlan(
            standalone_query="供应商的情况是怎样的",
            sub_queries=[
                "供应商管理",
                "供应商评估流程",
                "供应商合作政策",
            ],
        )
        self.assertEqual(
            [
                "供应商管理",
                "供应商评估流程",
                "供应商合作政策",
                "供应商的情况是怎样的",
            ],
            plan.normalized_sub_queries(),
        )

        # 已满 4：保住首个核心词，末位换成 standalone
        full = QueryPlan(
            standalone_query="整句兜底",
            sub_queries=["核心A", "侧面B", "侧面C", "侧面D"],
        )
        self.assertEqual(
            ["核心A", "侧面B", "侧面C", "整句兜底"],
            full.normalized_sub_queries(),
        )

    def test_query_planner_falls_back_when_model_unavailable(self) -> None:
        plan = QueryPlanner(_FakeChatModel(None)).build("如何申请年假")
        self.assertEqual("llm_unavailable", plan.notes)
        self.assertEqual("如何申请年假", plan.standalone_query)

    def test_history_text_keeps_latest_entries(self) -> None:
        history = [{"role": "user", "content": f"q{i}"} for i in range(8)]
        rendered = _history_text(history, max_turns=3)
        self.assertNotIn("q0", rendered)
        self.assertIn("q7", rendered)
        self.assertEqual(2, rendered.count("\n"))

    def test_rrf_fuses_and_deduplicates(self) -> None:
        result = rrf_fuse([[1, 2, 3], [2, 3, 4]], k=60)
        self.assertEqual([2, 3, 1, 4], [item for item, _ in result])
        self.assertAlmostEqual(1 / 62 + 1 / 61, result[0][1])

    def test_rrf_respects_weights_and_rejects_mismatch(self) -> None:
        result = rrf_fuse([[1, 2], [2, 3]], weights=[2.0, 1.0])
        self.assertEqual(2, result[0][0])
        self.assertAlmostEqual(2 / 62 + 1 / 61, result[0][1])
        with self.assertRaises(ValueError):
            rrf_fuse([[1], [2]], weights=[1.0])

    def test_parent_child_preserves_hierarchy(self) -> None:
        parents = split_markdown_parent_child(
            "# 甲\n\n正文甲\n\n## 乙\n\n正文乙\n"
        )
        self.assertEqual(["甲", "乙"], [parent.title for parent in parents])
        self.assertEqual(f"甲{TITLE_SEP}乙", parents[1].title_path)
        self.assertEqual(parents[0].children[0].parent_id, parents[0].id)

    def test_parent_child_uses_deeper_headers_as_child_boundaries(self) -> None:
        parents = split_markdown_parent_child(
            "## 二\n\n### 三一\n\n内容一\n\n### 三二\n\n内容二\n"
        )
        children = parents[0].children
        self.assertEqual(3, len(children))
        self.assertEqual(f"二{TITLE_SEP}三一", children[1].title_path)

    def test_citation_sanitizer_removes_only_out_of_range_numbers(self) -> None:
        cleaned, invalid = sanitize_citations("答案 [1][3] [9]", max_index=3)
        self.assertEqual("答案 [1][3]", cleaned)
        self.assertEqual([9], invalid)


if __name__ == "__main__":
    unittest.main()
