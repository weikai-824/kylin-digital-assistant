"""文档解析（Docling）：仅支持 PDF / DOCX → Markdown。

OA 同步与知识库 LOADER_DICT 统一走本模块。
PDF：版面 + 表格 + OCR；图区再补 RapidOCR（字太少可选用 Granite-Docling-258M）。
DOCX：结构化转换，内嵌图走同一套图区补字。
不做清洗、切分。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

# Docling 的 PDF 版面模型用 torch.compile(inductor)，在无 MSVC cl.exe 的 Windows 上会编译失败并 abort。
# 直接禁用 dynamo，让 torch.compile 变成 no-op（纯 eager），彻底绕开 inductor 编译。
try:
    import torch._dynamo as _torch_dynamo
    _torch_dynamo.config.disable = True
    _torch_dynamo.config.suppress_errors = True
except Exception:
    pass

PathLike = Union[str, Path]

SUPPORTED_PARSE_EXTS = {".pdf", ".docx"}


@dataclass
class DoclingParseResult:
    """单次解析结果。"""

    source: str
    markdown: str
    page_count: Optional[int] = None
    ocr_mode: Optional[str] = None
    pictures: int = 0
    pictures_ocr: int = 0
    pictures_vlm: int = 0


class DoclingDocumentLoader:
    """LangChain 兼容加载器：.pdf / .docx。"""

    def __init__(self, file_path: str, **kwargs):
        self.file_path = file_path

    def load(self):
        from kylin.platform.runtime.documents import Document

        result = parse_document(self.file_path)
        text = (result.markdown or "").strip()
        if not text:
            return []
        return [
            Document(
                page_content=text,
                metadata={"source": Path(self.file_path).name},
            )
        ]



def guess_pdf_ocr_mode(file_path: PathLike) -> str:
    """文字页用 pdf_aware，截图页用 full_page，避免文字 PDF 被整页 OCR 叠字。"""
    try:
        import fitz
    except ImportError:
        return "full_page"
    path = Path(file_path)
    doc = fitz.open(path)
    n = max(len(doc), 1)
    textish = 0
    for page in doc:
        t = (page.get_text("text") or "").strip()
        if len(t) >= 80:
            textish += 1
    doc.close()
    if textish / n >= 0.6:
        return "pdf_aware"
    return "full_page"


def _build_converter(
    *,
    do_ocr: bool = True,
    do_table_structure: bool = True,
    table_mode: str = "accurate",
    ocr_lang: Optional[list[str]] = None,
    ocr_mode: str = "full_page",
    artifacts_path: Optional[PathLike] = None,
):
    """构造只允许 PDF/DOCX 的 DocumentConverter。"""
    import os

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        OcrMode,
        PdfPipelineOptions,
        RapidOcrOptions,
        TableFormerMode,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    table_former = (
        TableFormerMode.FAST
        if str(table_mode).lower() == "fast"
        else TableFormerMode.ACCURATE
    )

    mode_map = {
        "full_page": OcrMode.FULL_PAGE,
        "layout_regions": OcrMode.LAYOUT_REGIONS,
        "pdf_aware": OcrMode.PDF_AWARE_LAYOUT_REGIONS,
        "default": OcrMode.DEFAULT,
    }
    ocr_mode_enum = mode_map.get(str(ocr_mode).lower(), OcrMode.FULL_PAGE)

    ocr_options = RapidOcrOptions(
        lang=ocr_lang or ["chinese"],
        backend="onnxruntime",
        mode=ocr_mode_enum,
    )

    art = artifacts_path or os.environ.get("DOCLING_ARTIFACTS_PATH") or None
    pipeline_kwargs = {
        "do_ocr": do_ocr,
        "do_table_structure": do_table_structure,
        "ocr_options": ocr_options,
        "table_structure_options": TableStructureOptions(mode=table_former),
        "generate_picture_images": True,
        "images_scale": 2.0,
    }
    if art:
        pipeline_kwargs["artifacts_path"] = str(art)

    pipeline_options = PdfPipelineOptions(**pipeline_kwargs)
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        },
    )


def parse_document(
    file_path: PathLike,
    *,
    do_ocr: bool = True,
    do_table_structure: bool = True,
    table_mode: str = "accurate",
    ocr_lang: Optional[list[str]] = None,
    ocr_mode: str = "auto",
    artifacts_path: Optional[PathLike] = None,
    picture_ocr: bool = True,
    picture_vlm: bool = False,
) -> DoclingParseResult:
    """用 Docling 解析 PDF/DOCX 为 Markdown，返回 DoclingParseResult。

    参数：
    - file_path: 本地 .pdf / .docx 路径
    - do_ocr: PDF 是否做版面 OCR（DOCX 走结构化读取，一般不依赖此项）
    - do_table_structure: PDF 表格结构识别开关
    - table_mode: accurate（默认）或 fast
    - ocr_lang: RapidOCR 语言，默认 ["chinese"]
    - ocr_mode: auto（默认，按页文字量自动分流）/ full_page / layout_regions / pdf_aware / default
    - artifacts_path: 模型目录；默认读环境变量 DOCLING_ARTIFACTS_PATH
    - picture_ocr: 对图区再跑 RapidOCR 补字
    - picture_vlm: 图区 OCR 字太少时用 Granite-Docling-258M；默认关闭（CPU 环境极慢）

    返回包含 source / markdown / page_count / ocr_mode 及图片统计的解析结果。
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    ext = path.suffix.lower()
    if ext not in SUPPORTED_PARSE_EXTS:
        raise ValueError(f"仅支持 {sorted(SUPPORTED_PARSE_EXTS)}，收到: {ext}")

    resolved_ocr_mode = ocr_mode
    if str(ocr_mode).lower() == "auto":
        resolved_ocr_mode = guess_pdf_ocr_mode(path) if ext == ".pdf" else "full_page"

    converter = _build_converter(
        do_ocr=do_ocr,
        do_table_structure=do_table_structure,
        table_mode=table_mode,
        ocr_lang=ocr_lang,
        ocr_mode=resolved_ocr_mode,
        artifacts_path=artifacts_path,
    )
    result = converter.convert(str(path))
    doc = result.document

    pictures = pictures_ocr = pictures_vlm = 0
    if picture_ocr or picture_vlm:
        from kylin.adapters.outbound.knowledge_base.parse.picture_text import enrich_pictures

        art = str(artifacts_path) if artifacts_path else None
        pictures, pictures_ocr, pictures_vlm = enrich_pictures(
            doc,
            artifacts_path=art,
            do_ocr=picture_ocr,
            do_vlm=picture_vlm,
        )

    markdown = (
        doc.export_to_markdown(
            image_placeholder="",
            allowed_meta_names={"description"},
        )
        or ""
    )

    if ext == ".docx":
        # Docling 判定标题只认样式名里的 "heading"，中文自定义标题样式（如
        # "标准文件_章标题"）会被降级为正文；此处按 Word 的 outlineLvl 重建标题层级。
        # 不满足启用条件时原样返回，详见 parse/docx_headings.py。
        from kylin.adapters.outbound.knowledge_base.parse.docx_headings import (
            patch_docx_markdown,
        )

        markdown = patch_docx_markdown(markdown, path)

    page_count = None
    try:
        page_count = len(doc.pages) if getattr(doc, "pages", None) is not None else None
    except Exception:
        page_count = None

    return DoclingParseResult(
        source=str(path.resolve()),
        markdown=markdown,
        page_count=page_count,
        ocr_mode=resolved_ocr_mode if ext == ".pdf" else None,
        pictures=pictures,
        pictures_ocr=pictures_ocr,
        pictures_vlm=pictures_vlm,
    )


def parse_pdf(
    file_path: PathLike,
    **kwargs,
) -> DoclingParseResult:
    """解析 PDF，返回 DoclingParseResult（统一走 parse_document）。"""
    path = Path(file_path)
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"parse_pdf 仅支持 PDF，收到: {path.suffix}")
    return parse_document(path, **kwargs)


def parse_docx(
    file_path: PathLike,
    **kwargs,
) -> DoclingParseResult:
    """解析 DOCX，返回 DoclingParseResult（统一走 parse_document）。"""
    path = Path(file_path)
    if path.suffix.lower() != ".docx":
        raise ValueError(f"parse_docx 仅支持 DOCX，收到: {path.suffix}")
    return parse_document(path, **kwargs)


def parse_pdf_to_markdown(file_path: PathLike, **kwargs) -> str:
    """解析 PDF 并直接返回 Markdown 文本。"""
    return parse_pdf(file_path, **kwargs).markdown


def parse_document_to_markdown(file_path: PathLike, **kwargs) -> str:
    """解析 PDF/DOCX 并直接返回 Markdown 文本。"""
    return parse_document(file_path, **kwargs).markdown


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Docling PDF/DOCX → Markdown")
    parser.add_argument("file", help="PDF 或 DOCX 路径")
    parser.add_argument("-o", "--output", help="写出 Markdown 路径")
    parser.add_argument("--no-ocr", action="store_true", help="关闭 PDF 版面 OCR")
    parser.add_argument("--no-table", action="store_true", help="关闭表格结构识别")
    parser.add_argument("--no-picture-ocr", action="store_true", help="关闭图区 RapidOCR")
    parser.add_argument(
        "--picture-vlm",
        action="store_true",
        help="开启图区 Granite-Docling（CPU 上很慢，默认关闭）",
    )
    parser.add_argument(
        "--table-mode",
        choices=["accurate", "fast"],
        default="accurate",
    )
    parser.add_argument(
        "--ocr-mode",
        choices=["auto", "full_page", "layout_regions", "pdf_aware", "default"],
        default="auto",
        help="PDF OCR 区域策略，默认 auto（文字页 pdf_aware，截图页 full_page）",
    )
    parser.add_argument("--artifacts-path", default=None)
    args = parser.parse_args()

    try:
        parsed = parse_document(
            args.file,
            do_ocr=not args.no_ocr,
            do_table_structure=not args.no_table,
            table_mode=args.table_mode,
            ocr_mode=args.ocr_mode,
            artifacts_path=args.artifacts_path,
            picture_ocr=not args.no_picture_ocr,
            picture_vlm=args.picture_vlm,
        )
    except Exception as e:
        print(f"解析失败: {e}", file=sys.stderr)
        sys.exit(1)

    header = (
        f"# source: {parsed.source}\n"
        f"# pages: {parsed.page_count}\n"
        f"# ocr_mode: {parsed.ocr_mode}\n"
        f"# pictures: {parsed.pictures} ocr={parsed.pictures_ocr} vlm={parsed.pictures_vlm}\n\n"
    )
    text = header + parsed.markdown
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(
            f"已写入 {out} （markdown 长度={len(parsed.markdown)} "
            f"ocr_mode={parsed.ocr_mode} pic={parsed.pictures} "
            f"ocr={parsed.pictures_ocr} vlm={parsed.pictures_vlm}）"
        )
    else:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")
