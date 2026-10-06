"""评估公共工具：结果目录 / 均值 / sys.path 注入。

runner.py 与 answer_runner.py 原各有一份重复实现（default_results_dir / _mean /
sys.path 注入），抽取到本模块统一维护。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable


def default_repo_root() -> Path:
    """仓库根。

    本包固定位于 <repo>/kylin/adapters/inbound/jobs/eval，故取 parents[5]。
    历史教训：曾在 dataset.py / answer_dataset.py 误写 parents[7]，导致
    IndexError 使两套评估完全无法启动。路径层级只允许在本文件计算一次，
    其余模块一律调用本函数，避免目录移动后索引再次漂移。
    """
    return Path(__file__).resolve().parents[5]


def default_eval_dir() -> Path:
    """金标与评估结果所在目录：<repo>/data/eval。"""
    return default_repo_root() / "data" / "eval"


def default_results_dir() -> Path:
    """评估结果目录：data/eval/results（gitignore，只保留最新一次）。"""
    return default_eval_dir() / "results"


def mean(xs: Iterable[float]) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def setup_sys_path() -> None:
    """把仓库根放入 sys.path（CLI 直接运行 eval 时必需）。"""
    root = default_repo_root()
    os.environ.setdefault("KYLIN_ROOT", os.environ.get("KYLIN_ROOT", "."))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
