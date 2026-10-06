"""知识问答纯逻辑回归测试，不连接外部服务。"""

from __future__ import annotations

from kylin.applications.knowledge_qa.context import assemble_context
from kylin.applications.knowledge_qa.evidence import is_weak_evidence, top_relevance
from kylin.applications.knowledge_qa.grounding import (
    ThinkStreamFilter,
    is_model_refusal,
    strip_think,
)
from kylin.adapters.outbound.knowledge_base.retrieve.rerank import rerank_document_text
from kylin.ports.data.knowledge_search import KnowledgeDocument


def test_context_keeps_source_numbering_and_section() -> None:
    docs = [
        KnowledgeDocument(
            page_content="  正文内容  ",
            metadata={"source": "手册A", "title_path": "第一章"},
            document_id="doc-1",
        )
    ]

    assert assemble_context(docs) == "[1] 来源：手册A | 章节：第一章\n正文内容"


def test_context_uses_stable_source_fallbacks() -> None:
    assert assemble_context([{"metadata": {"oa_doc_id": "oa1"}, "page_content": "x"}]) == (
        "[1] 来源：oa1\nx"
    )
    assert assemble_context([{"metadata": {}, "page_content": "x"}]) == (
        "[1] 来源：未知来源\nx"
    )


def test_evidence_threshold_boundaries_are_explicit() -> None:
    assert top_relevance([{"metadata": {"relevance_score": "0.42"}}]) == 0.42
    assert top_relevance([{"metadata": {}}]) is None
    assert is_weak_evidence(0.2, 0.2, 0.5)
    assert not is_weak_evidence(0.5, 0.2, 0.5)
    assert not is_weak_evidence(0.1, 0.2, 0.5)


def test_rerank_document_text_includes_source_and_chapter() -> None:
    doc = {
        "page_content": "点击设置绑定邮箱",
        "metadata": {
            "source": "OA办公协同平台操作手册之邮箱绑定.pdf",
            "title_path": "1. 邮箱登录",
        },
    }
    text = rerank_document_text(doc)
    assert "【文档】OA办公协同平台操作手册之邮箱绑定.pdf" in text
    assert "【章节】1. 邮箱登录" in text
    assert text.endswith("点击设置绑定邮箱")


def test_rerank_document_text_falls_back_to_body_only() -> None:
    assert rerank_document_text({"page_content": "纯正文", "metadata": {}}) == "纯正文"


def test_strip_think_removes_closed_and_unclosed_blocks() -> None:
    raw = "前缀<think>内部推理</think>后缀"
    assert strip_think(raw) == "前缀后缀"
    assert strip_think("答案<thinking>未闭合") == "答案"
    # oa-llm 探针形态：无开标签，仅有闭标签分隔思考与答案
    assert strip_think("推理过程</think>最终答案") == "最终答案"
    probe = (
        "好的，用户问的是关于ONES的东西。首先，我需要查看参考资料。"
        "确保直接输出答案。\n</think>\n\n"
        "ONES平台支持成员自行绑定MFA设备并开启两步验证。"
    )
    assert strip_think(probe) == "ONES平台支持成员自行绑定MFA设备并开启两步验证。"


def test_think_stream_filter_streams_after_think_close() -> None:
    filt = ThinkStreamFilter()
    assert filt.feed("好的，用户问的是") == ""
    assert filt.feed("关于ONES。\n</think>\n\n可见答案") == "可见答案"
    assert filt.feed("继续。") == "继续。"
    assert filt.finalize() == ""

    # 闭标签与答案分片到达
    split = ThinkStreamFilter()
    assert split.feed("裸思考过程</think>") == ""
    assert split.feed("\n\n最终答复") == "最终答复"
    assert split.finalize() == ""


def test_is_model_refusal_matches_prompt_refusal_phrases() -> None:
    # prompt 规则 3 的固定句（LLM 生成侧软拒答的典型输出）
    assert is_model_refusal("根据已知信息无法回答该问题。") is True
    assert is_model_refusal("根据已知信息无法回答该问题") is True
    # REFUSAL_NO_EVIDENCE 核心句（LLM 偶尔转述拒答常量）
    assert is_model_refusal(
        "根据当前知识库未找到足以回答该问题的依据，无法给出确切答复。"
    ) is True
    # 空答案不算
    assert is_model_refusal("") is False
    assert is_model_refusal(None) is False  # type: ignore[arg-type]
    # 长回答即使含"无法回答"字样也不判拒答（防误伤：引用/解释场景）
    long_answer = (
        "出差申请需通过费控系统提交：进入我的申请事项，选择差旅申请单，"
        "填写信息后提交送审。若提交报错，请先保存草稿再重新提交；"
        "确实无法回答该问题所在表单中的个别字段时，请联系部门管理员协助确认。"
    )
    assert is_model_refusal(long_answer) is False
    # 正常短回答不含拒答句式
    assert is_model_refusal("目前支持的系统包括人力资源、ERP、CRM 和费控。") is False


def test_think_stream_filter_plain_answer_still_returned_on_finalize() -> None:
    filt = ThinkStreamFilter()
    assert filt.feed("根据资料，邮箱绑定步骤如下") == ""
    assert filt.finalize() == "根据资料，邮箱绑定步骤如下"


def test_progress_status_text_covers_all_stages() -> None:
    from kylin.ports.application.knowledge_qa import (
        PROGRESS_GENERATING,
        PROGRESS_PLANNING,
        PROGRESS_RETRIEVING,
        PROGRESS_STATUS_TEXT,
    )

    assert PROGRESS_STATUS_TEXT[PROGRESS_PLANNING]
    assert PROGRESS_STATUS_TEXT[PROGRESS_RETRIEVING]
    assert PROGRESS_STATUS_TEXT[PROGRESS_GENERATING]


def test_retriever_merges_standalone_and_fallbacks_when_subs_empty() -> None:
    from kylin.applications.knowledge_qa.query_plan.schema import QueryPlan
    from kylin.applications.knowledge_qa.retrieval import KnowledgeRetriever
    from kylin.ports.data.knowledge_search import KnowledgeDocument

    calls: list[str] = []

    class _FakePort:
        def search(
            self,
            query,
            *,
            knowledge_base_name,
            top_k,
            score_threshold,
            dept=None,
            dept_exempt=False,
        ):
            calls.append(query)
            if query == "供应商管理":
                return [
                    KnowledgeDocument(
                        page_content="正文",
                        metadata={"doc_id": "d1", "score": 0.9},
                        document_id="d1",
                        score=0.9,
                    )
                ]
            return []

    retriever = KnowledgeRetriever(_FakePort())
    plan_hit = QueryPlan(
        standalone_query="供应商的情况是怎样的",
        sub_queries=["供应商管理", "供应商评估流程"],
    )
    docs = retriever.search(
        plan_hit,
        knowledge_base_name="oa_ma",
        top_k=5,
        score_threshold=2.0,
    )
    assert set(calls) == {
        "供应商管理",
        "供应商评估流程",
        "供应商的情况是怎样的",
    }
    assert len(docs) == 1
    assert docs[0].document_id == "d1"

    calls.clear()
    plan_miss = QueryPlan(
        standalone_query="原问兜底",
        sub_queries=["空侧面A", "空侧面B"],
    )
    docs = retriever.search(
        plan_miss,
        knowledge_base_name="oa_ma",
        top_k=5,
        score_threshold=2.0,
    )
    # 规范化为 [空侧面A, 空侧面B, 原问兜底]；均空后再兜底搜 standalone
    assert calls.count("空侧面A") == 1
    assert calls.count("空侧面B") == 1
    assert calls.count("原问兜底") == 2
    assert calls[-1] == "原问兜底"
    assert docs == []
