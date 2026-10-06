"""从图区补字：先 RapidOCR，字太少再可选 Granite-Docling-258M。

不替换 Docling 主解析；只给 PictureItem 写入 description，供 Markdown 导出。

写入的图区文本会夹一层来源标记（``<!-- ocr:<来源> --> ... <!-- /ocr -->``），
供清洗/切分等下游「按来源分治」；标记不改变文本内容，详见 ``wrap_ocr_text``。
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

from PIL import Image as PILImage

logger = logging.getLogger(__name__)

_OCR_THIN_CHARS = 16
_MIN_SIDE = 80
_VLM_MIN_SIDE = 120
_VLM_MAX_PER_DOC = 8

# OCR 预处理参数：截图模糊是误读第一根源，放大 + 对比度增强后再送识别。
_MIN_OCR_SCALE = 240  # 放大目标：短边至少这么多像素
_MAX_OCR_SCALE = 3.0  # 放大上限，防止超大图无谓放大
_CLAHE_CLIP = 2.0

_ocr_lock = threading.Lock()
_ocr_engine = None
_vlm_lock = threading.Lock()
_vlm_converter = None
_vlm_unavailable = False

# ---------------------------------------------------------------- 来源标记
# 背景（2026-09-10 实测，详见 data/eval/parse_eval/results/REPORT.md 第 7 节）：
# 截图型 OA 手册里，图区 OCR 贡献了正文的 40%~45%，但它在 Markdown 里与文字层
# **完全同质**（都是普通段落），下游无法区分来源。后果有二：
#   1. 清洗层只能对全文用同一套保守规则，OCR 碎片（截图里的按钮名/字段名，
#      实测占清洗后行数的 52%~73%）与 OCR 误读只能靠零散规则去打补丁；
#   2. 噪声归因做不了——分不清一条噪声来自文字层还是扫描/OCR。
# 这里给图区 OCR 文本夹一层来源标记，让下游可以「按来源分治」。
#
# 形态选择：HTML 注释。理由：Markdown 渲染时不可见；现有清洗规则只精确匹配
# `<!-- image -->`，不会误删；对纯文本处理链路也只是一行短字符串。
# 标记只描述来源，不改变任何内容——本步不做任何删除。
OCR_MARK_OPEN = "<!-- ocr:{src} -->"
OCR_MARK_CLOSE = "<!-- /ocr -->"


def wrap_ocr_text(text: str, source: str) -> str:
    """给图区文本加来源标记（幂等：已带标记则原样返回；空文本不加）。"""
    if not text or not text.strip():
        return text
    head = text.lstrip()
    if head.startswith("<!-- ocr:") and text.rstrip().endswith(OCR_MARK_CLOSE):
        return text
    return f"{OCR_MARK_OPEN.format(src=source)}\n{text}\n{OCR_MARK_CLOSE}"


def _artifacts_root(artifacts_path: Optional[str]) -> Optional[Path]:
    raw = artifacts_path or os.environ.get("DOCLING_ARTIFACTS_PATH")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def _rapidocr_engine(artifacts_path: Optional[str]):
    """单例懒加载 RapidOCR 引擎（线程安全）；artifacts 目录下有模型文件时优先使用。"""
    global _ocr_engine
    if _ocr_engine is not None:
        return _ocr_engine
    with _ocr_lock:
        if _ocr_engine is not None:
            return _ocr_engine
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import EngineType

        root = _artifacts_root(artifacts_path)
        params = {
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Cls.engine_type": EngineType.ONNXRUNTIME,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
        }
        if root is not None:
            ocr_dir = root / "RapidOcr"
            det = ocr_dir / "PP-OCRv6_det_small.onnx"
            cls = ocr_dir / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
            rec = ocr_dir / "PP-OCRv6_rec_small.onnx"
            if det.is_file():
                params["Det.model_path"] = str(det)
            if cls.is_file():
                params["Cls.model_path"] = str(cls)
            if rec.is_file():
                params["Rec.model_path"] = str(rec)
        _ocr_engine = RapidOCR(params=params)
        return _ocr_engine


def _preprocess_for_ocr(image: PILImage.Image) -> PILImage.Image:
    """截图 OCR 前预处理：LANCZOS 放大 + 灰度 + CLAHE 对比度增强。

    截图文字模糊（低分辨率 + 低对比度）是 OCR 误读的第一根源；
    放大后小截图也能跨过 _MIN_SIDE 门槛被识别。
    纯 CPU 操作，单图毫秒级。
    """
    import cv2
    import numpy as np

    rgb = image.convert("RGB")
    w, h = rgb.size
    short_side = min(w, h)
    if short_side < _MIN_OCR_SCALE:
        scale = min(_MIN_OCR_SCALE / short_side, _MAX_OCR_SCALE)
        rgb = rgb.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))), PILImage.LANCZOS
        )
    gray = np.asarray(rgb.convert("L"), dtype=np.uint8)
    gray = cv2.createCLAHE(
        clipLimit=_CLAHE_CLIP, tileGridSize=(8, 8)
    ).apply(gray)
    # RapidOCR 需要 3 通道输入，灰度拉伸回 RGB
    rgb_enhanced = np.stack([gray, gray, gray], axis=-1)
    return PILImage.fromarray(rgb_enhanced)


def ocr_image(image: PILImage.Image, artifacts_path: Optional[str] = None) -> str:
    """对一张图做中文 RapidOCR，返回按行拼接的文本。"""
    import numpy as np

    prepared = _preprocess_for_ocr(image)
    if min(prepared.size) < _MIN_SIDE:
        return ""
    engine = _rapidocr_engine(artifacts_path)
    result = engine(np.array(prepared))
    txts = getattr(result, "txts", None)
    if not txts:
        return ""
    lines = [str(t).strip() for t in txts if t and str(t).strip()]
    return "\n".join(lines)


def granite_docling_available(artifacts_path: Optional[str] = None) -> bool:
    root = _artifacts_root(artifacts_path)
    if root is None:
        return False
    return any(root.glob("*granite-docling*")) or any(root.glob("*granite_docling*"))


def _vlm_converter_cached(artifacts_path: Optional[str]):
    """单例懒加载 Granite-Docling VLM 转换器；模型缺失时置 unavailable 并返回 None。"""
    global _vlm_converter, _vlm_unavailable
    if _vlm_unavailable:
        return None
    if _vlm_converter is not None:
        return _vlm_converter
    with _vlm_lock:
        if _vlm_unavailable:
            return None
        if _vlm_converter is not None:
            return _vlm_converter
        if not granite_docling_available(artifacts_path):
            logger.warning(
                "未找到 Granite-Docling-258M，跳过图区 VLM。"
                "预下载: docling-tools models download granitedocling -o %s",
                artifacts_path or os.environ.get("DOCLING_ARTIFACTS_PATH") or "<models dir>",
            )
            _vlm_unavailable = True
            return None
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import VlmPipelineOptions
        from docling.datamodel.vlm_model_specs import GRANITEDOCLING_TRANSFORMERS
        from docling.document_converter import DocumentConverter, ImageFormatOption
        from docling.pipeline.vlm_pipeline import VlmPipeline

        root = _artifacts_root(artifacts_path)
        vlm_opts = GRANITEDOCLING_TRANSFORMERS.model_copy(deep=True)
        vlm_opts.max_new_tokens = 512
        opts = VlmPipelineOptions(vlm_options=vlm_opts)
        if root is not None:
            opts.artifacts_path = str(root)
        _vlm_converter = DocumentConverter(
            allowed_formats=[InputFormat.IMAGE],
            format_options={
                InputFormat.IMAGE: ImageFormatOption(
                    pipeline_cls=VlmPipeline,
                    pipeline_options=opts,
                )
            },
        )
        return _vlm_converter


def vlm_image(image: PILImage.Image, artifacts_path: Optional[str] = None) -> str:
    """用 Granite-Docling-258M 读一张图，返回 Markdown。模型不在则空串。"""
    if min(image.size) < _VLM_MIN_SIDE:
        return ""
    converter = _vlm_converter_cached(artifacts_path)
    if converter is None:
        return ""
    rgb = image.convert("RGB")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            rgb.save(tmp_path, format="PNG")
        result = converter.convert(str(tmp_path))
        return (result.document.export_to_markdown(image_placeholder="") or "").strip()
    except Exception as e:
        logger.warning("Granite-Docling 读图失败: %s", e)
        return ""
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _picture_image(doc, item) -> Optional[PILImage.Image]:
    try:
        img = item.get_image(doc)
    except Exception:
        return None
    if img is None:
        return None
    return img


def enrich_pictures(
    doc,
    *,
    artifacts_path: Optional[str] = None,
    do_ocr: bool = True,
    do_vlm: bool = True,
) -> tuple[int, int, int]:
    """给文档里的 PictureItem 写入 description。

    返回 (图片数, RapidOCR 补上的数量, VLM 补上的数量)。
    """
    from docling_core.types.doc import PictureItem
    from docling_core.types.doc.common.meta import DescriptionMetaField
    from docling_core.types.doc.items.picture.meta import PictureMeta

    pictures = []
    for item, _level in doc.iterate_items(traverse_pictures=True):
        if isinstance(item, PictureItem):
            pictures.append(item)

    ocr_hits = 0
    thin = []
    if do_ocr:
        for item in pictures:
            img = _picture_image(doc, item)
            # 原图短边 < _MIN_SIDE / _MAX_OCR_SCALE 时，放大 3x 后仍不够门槛，直接跳过
            if img is None or min(img.size) < _MIN_SIDE / _MAX_OCR_SCALE:
                continue
            text = ocr_image(img, artifacts_path)
            if text:
                _set_description(item, text, "rapidocr", PictureMeta, DescriptionMetaField)
                ocr_hits += 1
            if len(text) < _OCR_THIN_CHARS:
                thin.append((item, img, len(text)))

    vlm_hits = 0
    if do_vlm and thin:
        thin.sort(key=lambda x: x[1].size[0] * x[1].size[1], reverse=True)
        for item, img, ocr_len in thin[:_VLM_MAX_PER_DOC]:
            if min(img.size) < _VLM_MIN_SIDE:
                continue
            vlm_text = vlm_image(img, artifacts_path)
            if len(vlm_text) > ocr_len:
                _set_description(
                    item, vlm_text, "granite-docling-258M", PictureMeta, DescriptionMetaField
                )
                vlm_hits += 1

    return len(pictures), ocr_hits, vlm_hits


def _set_description(item, text: str, created_by: str, PictureMeta, DescriptionMetaField):
    if item.meta is None:
        item.meta = PictureMeta()
    marked = wrap_ocr_text(text, created_by)
    item.meta.description = DescriptionMetaField(text=marked, created_by=created_by)
