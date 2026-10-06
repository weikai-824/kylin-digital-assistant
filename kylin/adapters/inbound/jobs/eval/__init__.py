"""评估：离线检索评估（金标在 data/eval，逻辑在本包）。"""

from kylin.adapters.inbound.jobs.eval.dataset import load_eval_dataset, validate_eval_row
from kylin.adapters.inbound.jobs.eval.metrics import (
    hit_at_k,
    hit_rate_at_k,
    mean_reciprocal_rank,
    recall_at_k,
)
from kylin.adapters.inbound.jobs.eval.runner import run_retrieval_eval

__all__ = [
    "load_eval_dataset",
    "validate_eval_row",
    "hit_at_k",
    "hit_rate_at_k",
    "mean_reciprocal_rank",
    "recall_at_k",
    "run_retrieval_eval",
]
