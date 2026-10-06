"""回答评估任务的数据集加载与校验。

与检索评估集（dataset.py）物理隔离：回答评估集不绑定 doc_id / 文件名 /
父块 id，只记录问题本身和人类对正确回答的期望，保证换知识库后仍可复用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kylin.adapters.inbound.jobs.eval.common import default_eval_dir

REQUIRED_FIELDS = (
    "qid",
    "query",
    "category",
    "expected_behavior",
)
ALLOWED_CATEGORIES = {"positive", "negative", "edge"}
ALLOWED_BEHAVIORS = {"answer", "refuse", "partial"}

# 各字段语义：
#   qid               题号（前缀建议 ans_ 与检索集 retrieve_ 区分）
#   query             问题原文
#   category          positive=知识库应能回答 / negative=应拒答 / edge=边界（部分可答）
#   expected_behavior answer / refuse / partial —— 系统最终输出应呈现的行为
#   expected_points   正确回答必须包含的关键事实点（列表，判定用关键词覆盖）
#   forbidden_points  回答中绝不允许出现的事实点（防幻觉 / 防张冠李戴）
#   allowed_sources   可接受引用来源（附加参考指标，可选；不参与核心打分）
#   notes             人工判据备注，不参与打分


def default_answer_eval_path() -> Path:
    """仓库内默认回答评估集路径。"""
    return default_eval_dir() / "oa_answer_eval.jsonl"


def _is_str_list(value: Any) -> bool:
    """str 列表（允许为空；空列表表示'无此项'）。"""
    return isinstance(value, list) and all(
        isinstance(x, str) and x.strip() for x in value
    )


def _nonempty_str_list(value: Any) -> bool:
    return _is_str_list(value) and bool(value)


def validate_answer_row(row: Dict[str, Any], *, line_no: int = 0) -> List[str]:
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

    category = row.get("category")
    if category not in ALLOWED_CATEGORIES:
        errors.append(f"{prefix}: category must be one of {sorted(ALLOWED_CATEGORIES)}")

    behavior = row.get("expected_behavior")
    if behavior not in ALLOWED_BEHAVIORS:
        errors.append(
            f"{prefix}: expected_behavior must be one of {sorted(ALLOWED_BEHAVIORS)}"
        )

    # positive/answer 必须给出可判定的要点；negative/refuse 不得给 expected_points
    if category == "positive":
        if not _nonempty_str_list(row.get("expected_points")):
            errors.append(f"{prefix}: positive row needs non-empty expected_points")
    if category == "negative":
        if behavior != "refuse":
            errors.append(f"{prefix}: negative row expected_behavior must be refuse")
        if row.get("expected_points"):
            errors.append(f"{prefix}: negative row should not have expected_points")

    if "expected_points" in row and not _is_str_list(row["expected_points"]):
        errors.append(f"{prefix}: expected_points must be string list")
    if "forbidden_points" in row and not _is_str_list(row["forbidden_points"]):
        errors.append(f"{prefix}: forbidden_points must be string list")
    if "allowed_sources" in row and not _is_str_list(row["allowed_sources"]):
        errors.append(f"{prefix}: allowed_sources must be string list")
    # notes 可选；若提供必须是字符串
    if "notes" in row and not isinstance(row.get("notes"), str):
        errors.append(f"{prefix}: notes must be string")

    return errors


def load_answer_dataset(
    path: Optional[Path] = None,
    *,
    strict: bool = True,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    加载 jsonl 回答评估集。

    返回 (rows, errors)。strict=True 且存在错误时抛 ValueError。
    """
    path = Path(path) if path else default_answer_eval_path()
    if not path.is_file():
        raise FileNotFoundError(f"回答评估集不存在: {path}")

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
            errors.extend(validate_answer_row(row, line_no=i))
            qid = row.get("qid")
            if isinstance(qid, str):
                if qid in seen_qid:
                    errors.append(f"L{i}: duplicate qid `{qid}`")
                seen_qid.add(qid)
            rows.append(row)

    if strict and errors:
        preview = "\n".join(errors[:20])
        more = f"\n... and {len(errors) - 20} more" if len(errors) > 20 else ""
        raise ValueError(f"回答评估集校验失败 ({len(errors)}):\n{preview}{more}")

    return rows, errors
