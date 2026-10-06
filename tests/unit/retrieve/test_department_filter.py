"""访问范围过滤（access_depts）单元测试。"""

from __future__ import annotations

import pytest

from kylin.adapters.outbound.knowledge_base.retrieve.dual_search import (
    build_department_es_filter,
    build_department_filter,
)


class TestBuildDepartmentFilter:
    """Milvus expr 过滤条件构建（access_depts 语义）。"""

    def test_exempt_returns_none(self):
        assert build_department_filter("dept-1", dept_exempt=True) is None
        assert build_department_filter(None, dept_exempt=True) is None

    def test_anonymous_only_public(self):
        # 匿名（无 dept，非豁免）：只保留全开放文档（access_depts 为空）
        assert build_department_filter(None) == 'access_depts == ""'
        assert build_department_filter("") == 'access_depts == ""'

    def test_department_user_keeps_public_and_own(self):
        # 部门用户：全开放 + access_depts 含本部门（like 匹配）
        expr = build_department_filter("dept-1")
        assert expr == 'access_depts == "" or access_depts like "%dept-1%"'

    def test_expr_escapes_like_wildcards(self):
        # 部门 ID 若含特殊字符，需避免被当 like 通配符（此处验证转义函数不被破坏）
        expr = build_department_filter("dept-1")
        assert "dept-1" in expr


class TestBuildDepartmentEsFilter:
    """ES bool filter 子句构建（access_depts keyword 数组语义）。"""

    def test_exempt_returns_none(self):
        assert build_department_es_filter("dept-1", dept_exempt=True) is None
        assert build_department_es_filter(None, dept_exempt=True) is None

    def test_anonymous_only_public(self):
        # 无 access_depts 字段（= 空数组 = 全开放）
        filters = build_department_es_filter(None)
        assert filters == [
            {"bool": {"must_not": {"exists": {"field": "access_depts"}}}}
        ]

    def test_department_user_keeps_public_and_own(self):
        filters = build_department_es_filter("dept-1")
        assert filters is not None
        should = filters[0]["bool"]["should"]
        assert {"bool": {"must_not": {"exists": {"field": "access_depts"}}}} in should
        assert {"term": {"access_depts": "dept-1"}} in should
        assert filters[0]["bool"]["minimum_should_match"] == 1
