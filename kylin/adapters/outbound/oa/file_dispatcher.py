"""按类型分派处理 OA 下载的文件（docFile 主文档 / attList 附件）。

取代旧的"只收 .pdf/.docx、其余跳过"过滤：先按 docType / 文件名定类型，
再按类型决定下不下载、怎么转换、是否入库。

检测优先级：有 docType 用 docType（docFile 主文档），否则用文件名扩展名（attList 附件）。
- pdf      → 下载 + 去水印 + 入库
- docx     → 下载 + 入库
- office   → 下载 + magic bytes 嗅探子类（docx 入库；xlsx/pptx/unknown 跳过）
- xlsx/pptx→ 不下载，直接跳过并记录
- other/text（docType）→ 不下载，按"空文档"跳过
- 未知     → 不下载，跳过并记录
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from kylin.adapters.outbound.oa.pdf_watermark import strip_if_pdf_bytes
from kylin.ports.data.oa_sync import OAAttachmentDispatchResult

logger = logging.getLogger(__name__)

# 兼容旧名：出站实现与端口 DTO 共用同一形状
DispatchResult = OAAttachmentDispatchResult


def _ext_from_name(name: str) -> str:
    return Path(name).suffix.lower() if name else ""


def _safe_stem(name: str, fallback: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", name or "").strip("._") or fallback
    return safe[:80] or "file"


def _build_local_filename(doc_id: str, display_name: str) -> str:
    """display_name 已带正确扩展名；拼成 oa_{doc_id}_{stem}{suffix}。"""
    path = Path(display_name)
    return f"oa_{doc_id}_{_safe_stem(path.stem, display_name)}{path.suffix.lower()}"


def _sniff_office(content: bytes) -> str:
    """从 zip 内 [Content_Types].xml 判断 docx/pptx/xlsx；非 zip 或读不出返回 unknown。"""
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        ct = z.read("[Content_Types].xml").decode("utf-8", "ignore")
    except Exception:
        return "unknown"
    if "wordprocessingml" in ct:
        return "docx"
    if "presentationml" in ct:
        return "pptx"
    if "spreadsheetml" in ct:
        return "xlsx"
    return "unknown"


def _resolve_kind(item: Any) -> str:
    """(docType, 文件名扩展名) → kind。docType 优先，其次文件名。"""
    doc_type = str(item.get("docType") or "").lower()
    if doc_type == "pdf":
        return "pdf"
    if doc_type == "office":
        return "office"
    if doc_type in ("other", "text"):
        return "empty"
    name_ext = _ext_from_name(str(item.get("name") or item.get("filename") or ""))
    if name_ext == ".pdf":
        return "pdf"
    if name_ext == ".docx":
        return "docx"
    if name_ext == ".xlsx":
        return "xlsx"
    if name_ext == ".pptx":
        return "pptx"
    if name_ext:
        return "unknown"
    return "unknown"


def _base_name(item: Any, doc_name: str) -> str:
    """取原始文件名；docFile 无名时用文档标题兜底（扩展名由 handler 补）。"""
    name = str(item.get("name") or item.get("filename") or "").strip()
    if name:
        return name
    ext = str(item.get("extName") or "").strip().lstrip(".")
    return f"{doc_name}.{ext}" if ext else doc_name


def _handle_pdf(item, download_fn, doc_id, base_name) -> Optional[DispatchResult]:
    content = download_fn()
    display = base_name if base_name.lower().endswith(".pdf") else f"{base_name}.pdf"
    local_name = _build_local_filename(doc_id, display)
    content = strip_if_pdf_bytes(content, local_name)
    return DispatchResult(local_name, content, True, "pdf")


def _handle_docx(item, download_fn, doc_id, base_name) -> Optional[DispatchResult]:
    content = download_fn()
    display = base_name if base_name.lower().endswith(".docx") else f"{base_name}.docx"
    return DispatchResult(_build_local_filename(doc_id, display), content, True, "docx")


def _handle_office(item, download_fn, doc_id, base_name) -> Optional[DispatchResult]:
    content = download_fn()
    sub = _sniff_office(content)
    if sub == "docx":
        return DispatchResult(_build_local_filename(doc_id, f"{base_name}.docx"), content, True, "docx")
    logger.warning(f"跳过不支持的 office 子类型 doc_id={doc_id} sub={sub} name={base_name}")
    return None


def _handle_xlsx(item, download_fn, doc_id, base_name) -> Optional[DispatchResult]:
    logger.warning(f"跳过不支持的附件类型 xlsx doc_id={doc_id} name={base_name}")
    return None


def _handle_pptx(item, download_fn, doc_id, base_name) -> Optional[DispatchResult]:
    logger.warning(f"跳过不支持的附件类型 pptx doc_id={doc_id} name={base_name}")
    return None


def _handle_empty(item, download_fn, doc_id, base_name) -> Optional[DispatchResult]:
    logger.info(f"跳过空文档 doc_id={doc_id} name={base_name}")
    return None


def _handle_unknown(item, download_fn, doc_id, base_name) -> Optional[DispatchResult]:
    logger.warning(f"跳过无法识别类型的文件 doc_id={doc_id} name={base_name}")
    return None


_HANDLERS: dict[str, Callable[..., Optional[DispatchResult]]] = {
    "pdf": _handle_pdf,
    "docx": _handle_docx,
    "office": _handle_office,
    "xlsx": _handle_xlsx,
    "pptx": _handle_pptx,
    "empty": _handle_empty,
    "unknown": _handle_unknown,
}


def dispatch(
    item: Any,
    *,
    doc_id: str,
    doc_name: str,
    download_fn: Callable[[], bytes],
) -> Optional[OAAttachmentDispatchResult]:
    """按类型分派一个 OA 文件（附件或 docFile 主文档）。

    download_fn 由调用方注入（已闭包 file_id / module）；分派器内部决定是否调用它。
    返回 OAAttachmentDispatchResult 表示要入库；返回 None 表示跳过（已打日志）。
    """
    kind = _resolve_kind(item)
    base = _base_name(item, doc_name)
    handler = _HANDLERS[kind]
    return handler(item, download_fn, doc_id, base)
