"""入库层：编排落盘/解析/切分，并提供 DualIndex 写入适配。"""

from kylin.adapters.outbound.knowledge_base.ingest.dual_writer import DualIndexKBService, ensure_kb
from kylin.adapters.outbound.knowledge_base.ingest.run import (
    SUPPORTED_EXTS,
    delete_kb_files,
    ingest_file,
    load_document,
    write_kb_file,
)

__all__ = [
    "DualIndexKBService",
    "ensure_kb",
    "SUPPORTED_EXTS",
    "write_kb_file",
    "load_document",
    "ingest_file",
    "delete_kb_files",
]
