"""知识库服务（核心业务）：服务抽象 / 文件对象 / 路径 / 文档模型 / 视图聚合。"""

from kylin.adapters.outbound.knowledge_base.kb_service.base import (  # noqa: F401
    KBService,
    SupportedVSType,
)
from kylin.adapters.outbound.knowledge_base.kb_service.factory import KBServiceFactory
from kylin.adapters.outbound.knowledge_base.kb_service.files import (  # noqa: F401
    LOADER_DICT,
    SUPPORTED_EXTS,
    KnowledgeFile,
    files2docs_in_thread,
    get_LoaderClass,
    get_loader,
)
from kylin.adapters.outbound.knowledge_base.kb_service.models import DocumentWithVSId  # noqa: F401
from kylin.adapters.outbound.knowledge_base.kb_service.views import (  # noqa: F401
    get_kb_details,
    get_kb_file_details,
)
from kylin.adapters.outbound.knowledge_base.kb_service.paths import (  # noqa: F401
    get_doc_path,
    get_file_path,
    get_kb_path,
    get_vs_path,
    list_files_from_folder,
    list_kbs_from_folder,
    validate_kb_name,
)

__all__ = [
    "KBService",
    "KBServiceFactory",
    "SupportedVSType",
    "get_kb_details",
    "get_kb_file_details",
    "KnowledgeFile",
    "DocumentWithVSId",
    "files2docs_in_thread",
    "get_kb_path",
    "get_doc_path",
    "get_vs_path",
    "get_file_path",
    "list_files_from_folder",
    "list_kbs_from_folder",
    "validate_kb_name",
    "LOADER_DICT",
    "SUPPORTED_EXTS",
    "get_LoaderClass",
    "get_loader",
]
