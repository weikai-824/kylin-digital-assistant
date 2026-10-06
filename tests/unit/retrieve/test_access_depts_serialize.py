"""access_depts 序列化单元测试。"""

from __future__ import annotations

from kylin.adapters.outbound.knowledge_base.index.stores import (
    _access_depts_list,
    _serialize_access_depts,
)


class TestSerializeAccessDepts:
    def test_none_or_empty_is_public(self):
        assert _serialize_access_depts(None) == ""
        assert _serialize_access_depts([]) == ""
        assert _serialize_access_depts("") == ""

    def test_list_serializes_with_semicolons(self):
        assert _serialize_access_depts(["D1", "D2"]) == ";D1;D2;"
        assert _serialize_access_depts(["D1"]) == ";D1;"

    def test_already_serialized_string_kept(self):
        assert _serialize_access_depts(";D1;D2;") == ";D1;D2;"

    def test_bare_string_normalized(self):
        assert _serialize_access_depts("D1;D2") == ";D1;D2;"

    def test_empty_items_dropped(self):
        assert _serialize_access_depts(["D1", "", "D2"]) == ";D1;D2;"

    def test_es_list_from_serialized(self):
        assert _access_depts_list(";D1;D2;") == ["D1", "D2"]
        assert _access_depts_list("") == []
        assert _access_depts_list(None) == []
