"""知识库服务工厂：根据台账配置创建具体的知识库实现."""

from __future__ import annotations

from typing import Union

from kylin.adapters.outbound.mysql.database.repository.knowledge_base_repository import (
    load_kb_from_db,
)
from kylin.adapters.outbound.knowledge_base.kb_service.base import (
    KBService,
    SupportedVSType,
)


class KBServiceFactory:
    """创建和读取具体知识库服务，不承载知识库业务流程."""

    @staticmethod
    def get_service(
        kb_name: str,
        vector_store_type: Union[str, SupportedVSType] = SupportedVSType.DUAL,
        embed_model: str = None,
        kb_info: str = None,
    ) -> KBService:
        # embed_model 为 None 时由 KBService.__init__ 延迟解析默认值
        if isinstance(vector_store_type, str):
            key = (vector_store_type or "dual").upper()
            if not hasattr(SupportedVSType, key):
                raise ValueError(f"不支持的知识库类型 {vector_store_type}，仅支持 dual")
            vector_store_type = getattr(SupportedVSType, key)
        if vector_store_type != SupportedVSType.DUAL:
            raise ValueError(f"不支持的知识库类型 {vector_store_type}，仅支持 dual")
        from kylin.adapters.outbound.knowledge_base.ingest.dual_writer import DualIndexKBService

        return DualIndexKBService(
            knowledge_base_name=kb_name,
            embed_model=embed_model,
            kb_info=kb_info,
        )

    @staticmethod
    def get_service_by_name(kb_name: str) -> KBService | None:
        stored_name, vs_type, embed_model = load_kb_from_db(kb_name)
        if stored_name is None:
            return None
        return KBServiceFactory.get_service(kb_name, vs_type, embed_model)
