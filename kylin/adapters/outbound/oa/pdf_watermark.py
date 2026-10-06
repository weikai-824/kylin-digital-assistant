"""去除 OA 下载 PDF 的身份水印。

OA 给每页叠了一个额外内容流：一层薄 ``q`` 包装 + 一个 45° 旋转的平铺身份串
（工号 / 姓名 / 日期）。本模块识别并删除这些内容流。

迁移自 kylinsec-openapi-notes/tools/oa-sync-portable，原逻辑对着同一套 OA 验过，
仅新增 ``strip_pdf_watermarks_bytes`` 以适配本项目"下载到内存 bytes"的入库流程。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]


def _is_wrapper_q(raw: bytes) -> bool:
    return raw.strip() == b"q"


def _is_watermark_stream(raw: bytes) -> bool:
    if not raw:
        return False
    # 旋转叠层内容流以 Q 开头（与前置 q 配对），并使用约 45° 的 0.70711 系数。
    body = raw.lstrip()
    return body.startswith(b"Q") and b"0.70711" in raw


def _strip_doc(doc) -> int:
    """对已打开的 pymupdf 文档逐页删水印内容流，返回被修改的页数。"""
    changed_pages = 0
    for page in doc:
        xrefs = list(page.get_contents() or [])
        if len(xrefs) < 2:
            continue

        kept: list[int] = []
        dropped = False
        for xref in xrefs:
            raw = doc.xref_stream(xref) or b""
            if _is_wrapper_q(raw) or _is_watermark_stream(raw):
                dropped = True
                continue
            kept.append(xref)

        if not dropped:
            continue
        if not kept:
            kept = [max(xrefs, key=lambda x: len(doc.xref_stream(x) or b""))]

        if len(kept) == 1:
            page.set_contents(kept[0])
        else:
            merged = b"\n".join(doc.xref_stream(x) or b"" for x in kept)
            new_xref = doc.get_new_xref()
            doc.update_object(new_xref, "<<>>")
            doc.update_stream(new_xref, merged)
            page.set_contents(new_xref)
        changed_pages += 1
    return changed_pages


def strip_pdf_watermarks(path: PathLike, *, inplace: bool = True) -> bool:
    """去除 PDF 的 OA 身份水印，返回是否修改过文件。"""
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz  # PyMuPDF <1.24 只能用 fitz 这个导入名
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "pymupdf is required for PDF watermark removal; pip install pymupdf"
            ) from exc

    pdf_path = Path(path)
    if pdf_path.suffix.lower() != ".pdf" or not pdf_path.is_file():
        return False

    doc = fitz.open(pdf_path)
    try:
        if _strip_doc(doc) == 0:
            return False

        if inplace:
            tmp = pdf_path.with_suffix(pdf_path.suffix + ".tmp")
            doc.save(tmp, garbage=4, deflate=True, clean=True)
            doc.close()
            tmp.replace(pdf_path)
        else:
            doc.save(pdf_path, garbage=4, deflate=True, clean=True)
            doc.close()
        logger.info("stripped OA watermark file=%s", pdf_path)
        return True
    except Exception:
        doc.close()
        raise


def strip_pdf_watermarks_bytes(content: bytes) -> bytes:
    """对内存中的 PDF bytes 去水印；非 PDF 或无水印则原样返回。

    走临时文件复用 ``strip_pdf_watermarks`` 的已验证逻辑，避免重复实现。
    """
    if not content:
        return content
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        if strip_pdf_watermarks(tmp_path, inplace=True):
            return tmp_path.read_bytes()
        return content
    except Exception as exc:  # noqa: BLE001
        logger.warning("watermark strip skipped: %s", exc)
        return content
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def strip_if_pdf_bytes(content: bytes, filename: str) -> bytes:
    """按文件名判断是否 PDF，是则去水印，否则原样返回。"""
    if Path(filename).suffix.lower() != ".pdf":
        return content
    return strip_pdf_watermarks_bytes(content)
