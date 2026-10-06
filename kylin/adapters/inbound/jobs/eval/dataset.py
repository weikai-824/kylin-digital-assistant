"""检索评估任务的数据集加载与格式校验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kylin.adapters.inbound.jobs.eval.common import default_eval_dir

REQUIRED_FIELDS = (
    "qid",
    "query",
    "query_type",
    "source",
    "expected_section",
    "expected_snippet",
    "relevant_parent_doc_ids",
)
ALLOWED_TYPES = {"keyword", "semantic", "table_numeric", "negative"}


def default_eval_path() -> Path:
    """仓库内默认金标路径（检索评估集）。"""
    return default_eval_dir() / "oa_retrieve_eval.jsonl"


def validate_eval_row(row: Dict[str, Any], *, line_no: int = 0) -> List[str]:
    """校验单行，返回错误信息列表（空表示通过）。"""
    errors: List[str] = []
    prefix = f"L{line_no}" if line_no else (row.get("qid") or "?")

    for key in REQUIRED_FIELDS:
        if key not in row:
            errors.append(f"{prefix}: missing field `{key}`")

    qid = row.get("qid")
    if not qid or not isinstance(qid, str):
        errors.append(f"{prefix}: qid must be non-empty string")

    query = row.get("query")
    if not isinstance(query, str) or not query.strip():
        errors.append(f"{prefix}: query must be non-empty string")

    qtype = row.get("query_type")
    if qtype not in ALLOWED_TYPES:
        errors.append(f"{prefix}: query_type must be one of {sorted(ALLOWED_TYPES)}")

    ids = row.get("relevant_parent_doc_ids")
    if not isinstance(ids, list):
        errors.append(f"{prefix}: relevant_parent_doc_ids must be a list")
        ids = []
    elif any(not isinstance(x, str) or not x.strip() for x in ids):
        errors.append(f"{prefix}: relevant_parent_doc_ids items must be non-empty strings")

    if qtype == "negative":
        if row.get("source"):
            errors.append(f"{prefix}: negative row should have empty source")
        if row.get("expected_snippet"):
            errors.append(f"{prefix}: negative row should have empty expected_snippet")
        if ids:
            errors.append(f"{prefix}: negative row should have empty relevant_parent_doc_ids")
    else:
        if not row.get("source"):
            errors.append(f"{prefix}: positive row needs source")
        if not ids:
            errors.append(f"{prefix}: positive row needs relevant_parent_doc_ids")
        if len(ids) != len(set(ids)):
            errors.append(f"{prefix}: duplicate parent ids")

    return errors


def load_eval_dataset(
    path: Optional[Path] = None,
    *,
    strict: bool = True,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    加载 jsonl 评估集。

    返回 (rows, errors)。strict=True 且存在错误时抛 ValueError。
    """
    path = Path(path) if path else default_eval_path()
    if not path.is_file():
        raise FileNotFoundError(f"评估集不存在: {path}")

    rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen_qid = set()

    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"L{i}: invalid json ({e})")
                continue
            if not isinstance(row, dict):
                errors.append(f"L{i}: row must be object")
                continue
            errors.extend(validate_eval_row(row, line_no=i))
            qid = row.get("qid")
            if isinstance(qid, str):
                if qid in seen_qid:
                    errors.append(f"L{i}: duplicate qid `{qid}`")
                seen_qid.add(qid)
            rows.append(row)

    if strict and errors:
        preview = "\n".join(errors[:20])
        more = f"\n... and {len(errors) - 20} more" if len(errors) > 20 else ""
        raise ValueError(f"评估集校验失败 ({len(errors)}):\n{preview}{more}")

    return rows, errors
