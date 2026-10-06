"""知识库目录与台账的兼容展示视图."""

from __future__ import annotations

import os
from typing import Dict, List

from kylin.adapters.outbound.mysql.database.models.knowledge_base_model import KnowledgeBaseSchema
from kylin.adapters.outbound.mysql.database.repository.knowledge_file_repository import (
    get_file_access_depts,
    get_file_department_ids,
    get_file_detail,
)
from kylin.adapters.outbound.knowledge_base.kb_service.base import KBService
from kylin.adapters.outbound.knowledge_base.kb_service.factory import KBServiceFactory
from kylin.adapters.outbound.knowledge_base.kb_service.paths import (
    list_files_from_folder,
    list_kbs_from_folder,
)


def get_kb_details() -> List[Dict]:
    """知识库列表（磁盘目录 × MySQL 台账合并视图）。"""
    kbs_in_folder = list_kbs_from_folder()
    kbs_in_db: List[KnowledgeBaseSchema] = KBService.list_kbs()
    result = {}

    for kb in kbs_in_folder:
        result[kb] = {
            "kb_name": kb,
            "vs_type": "",
            "kb_info": "",
            "embed_model": "",
            "file_count": 0,
            "create_time": None,
            "in_folder": True,
            "in_db": False,
        }

    for kb_detail in kbs_in_db:
        kb_detail = kb_detail.model_dump()
        kb_name = kb_detail["kb_name"]
        kb_detail["in_db"] = True
        if kb_name in result:
            result[kb_name].update(kb_detail)
        else:
            kb_detail["in_folder"] = False
            result[kb_name] = kb_detail

    data = []
    for i, value in enumerate(result.values()):
        value["No"] = i + 1
        data.append(value)

    return data


def get_kb_file_details(kb_name: str) -> List[Dict]:
    """知识库文件列表（磁盘目录 × 台账合并视图）。"""
    kb = KBServiceFactory.get_service_by_name(kb_name)
    if kb is None:
        return []

    files_in_folder = list_files_from_folder(kb_name)
    files_in_db = kb.list_files()
    result = {}

    for doc in files_in_folder:
        result[doc] = {
            "kb_name": kb_name,
            "file_name": doc,
            "file_ext": os.path.splitext(doc)[-1],
            "docs_count": 0,
            "create_time": None,
            "in_folder": True,
            "in_db": False,
        }
    lower_names = {x.lower(): x for x in result}
    for doc in files_in_db:
        doc_detail = get_file_detail(kb_name, doc)
        if doc_detail:
            doc_detail["in_db"] = True
            if doc.lower() in lower_names:
                result[lower_names[doc.lower()]].update(doc_detail)
            else:
                doc_detail["in_folder"] = False
                result[doc] = doc_detail

    # 部门打标聚合：file_doc.meta_data（每文件第一个非空值）
    file_departments = get_file_department_ids(kb_name)
    file_access = get_file_access_depts(kb_name)
    for value in result.values():
        file_name = value.get("file_name", "")
        value["department_id"] = file_departments.get(file_name, "")
        # access_depts：空 = 全开放；非空 = 仅这些部门可见
        value["access_depts"] = file_access.get(file_name, [])

    data = []
    for i, value in enumerate(result.values()):
        value["No"] = i + 1
        data.append(value)

    return data
