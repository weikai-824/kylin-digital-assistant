"""孤儿标题节合并（_merge_orphan_title_sections）单测。

背景：解析层归属错位会产生"光标题"父块（标题在 A 节、内容全在 B 节），
检索命中光标题块时 LLM 拿不到正文。合并策略：孤儿标题行并入下一节开头。
"""

from kylin.adapters.outbound.knowledge_base.split.parent_child import (
    _merge_orphan_title_sections,
    split_markdown_parent_child,
)


def test_orphan_title_merged_into_next_section():
    """孤儿标题（正文为空）的标题行应并入下一节正文开头，不再单独成块。"""
    text = (
        "## 目前支持单点登录（免密码）的系统如下：\n"
        "\n"
        "## 目前支持统一待办的系统如下：\n"
        "\n"
        "- ERP系统（外部访问时，需登录VPN）\n"
        "- CRM（纷享销客）\n"
    )
    sections = _merge_orphan_title_sections(
        __import__(
            "kylin.adapters.outbound.knowledge_base.split.parent_child",
            fromlist=["_split_by_headers"],
        )._split_by_headers(text, {2})
    )
    assert len(sections) == 1, f"应合并为 1 节，实际 {len(sections)}: {sections}"
    title, level, title_path, body = sections[0]
    assert title == "目前支持统一待办的系统如下："
    assert "## 目前支持单点登录（免密码）的系统如下：" in body
    assert "- ERP系统" in body

    parents = split_markdown_parent_child(text)
    assert len(parents) == 1
    assert "单点登录" in parents[0].text
    assert "ERP系统" in parents[0].text


def test_consecutive_orphan_titles_cascade():
    """连续多个孤儿标题应级联合并进下一个有正文的节。"""
    text = (
        "## 第一部分 登录功能说明\n"
        "\n"
        "## 综合门户\n"
        "\n"
        "门户首页展示常用应用入口。\n"
    )
    sections = _merge_orphan_title_sections(
        __import__(
            "kylin.adapters.outbound.knowledge_base.split.parent_child",
            fromlist=["_split_by_headers"],
        )._split_by_headers(text, {2})
    )
    assert len(sections) == 1
    title, _level, _tp, body = sections[0]
    assert title == "综合门户"
    assert "## 第一部分 登录功能说明" in body
    assert "门户首页" in body


def test_trailing_orphan_title_kept():
    """文档末尾的孤儿标题无下一节可并，应原样保留（不丢内容）。"""
    text = (
        "## 有内容的节\n"
        "\n"
        "正文若干。\n"
        "\n"
        "## 感谢参与培训\n"
    )
    sections = _merge_orphan_title_sections(
        __import__(
            "kylin.adapters.outbound.knowledge_base.split.parent_child",
            fromlist=["_split_by_headers"],
        )._split_by_headers(text, {2})
    )
    assert len(sections) == 2
    assert sections[-1][0] == "感谢参与培训"


def test_normal_sections_untouched():
    """正常节（都有正文）不应被合并逻辑改变。"""
    text = (
        "## 甲\n"
        "\n"
        "甲的正文。\n"
        "\n"
        "## 乙\n"
        "\n"
        "乙的正文。\n"
    )
    sections = _merge_orphan_title_sections(
        __import__(
            "kylin.adapters.outbound.knowledge_base.split.parent_child",
            fromlist=["_split_by_headers"],
        )._split_by_headers(text, {2})
    )
    assert [s[0] for s in sections] == ["甲", "乙"]
    assert "甲的正文" in sections[0][3]
    assert "乙的正文" in sections[1][3]
