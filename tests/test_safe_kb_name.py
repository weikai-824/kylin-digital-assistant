"""safe_kb_name / 索引命名回归：Milvus 拒绝非 ASCII collection 名。"""

from __future__ import annotations

import unittest

from kylin.adapters.outbound.knowledge_base.index.stores import (
    children_collection_name,
    safe_kb_name,
)


class SafeKbNameTests(unittest.TestCase):
    def test_ascii_names_stay_stable(self) -> None:
        self.assertEqual("oa_manuals", safe_kb_name("oa_manuals"))
        self.assertEqual("oa_manuals_children", children_collection_name("oa_manuals"))

    def test_chinese_names_become_ascii_only(self) -> None:
        name = safe_kb_name("OA操作手册")
        self.assertRegex(name, r"^[a-z][a-z0-9_]*$")
        self.assertNotRegex(name, r"[^\x00-\x7f]")
        self.assertTrue(children_collection_name("OA操作手册").endswith("_children"))

    def test_same_chinese_name_is_stable(self) -> None:
        self.assertEqual(safe_kb_name("OA操作手册"), safe_kb_name("OA操作手册"))


if __name__ == "__main__":
    unittest.main()
