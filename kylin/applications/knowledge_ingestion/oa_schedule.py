"""OA 同步周期调度：常驻进程按间隔调用增量 sync。"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_STOP = threading.Event()
_RUNNING = threading.Lock()


def request_stop() -> None:
    """请求调度循环在下一检查点退出（供信号处理器调用）。"""
    _STOP.set()


def run_oa_sync_schedule(
    *,
    load_settings: Callable[[], Any],
    build_service: Callable[[], Any],
    sleep_fn: Callable[[float], None] = time.sleep,
    chunk_seconds: int = 60,
) -> None:
    """常驻循环：热读配置 → 增量 sync → 按 interval_hours 等待。

    启动条件：enabled 与 schedule_enabled 均为 true，否则抛出 ValueError。
    上一轮未结束时跳过本轮（单线程下主要由间隔语义保证；锁防重入）。
    单轮异常不退出进程；failed>0 打 warning。
    """
    _STOP.clear()
    settings = load_settings()
    _require_daemon_ready(settings)

    run_on_start = bool(getattr(settings, "run_on_start", True))
    if not run_on_start:
        logger.info("run_on_start=false，先等待一个间隔再同步")
        if not _sleep_until_next(load_settings, sleep_fn=sleep_fn, chunk_seconds=chunk_seconds):
            return

    while not _STOP.is_set():
        settings = load_settings()
        if not bool(getattr(settings, "enabled", False)):
            logger.error("enabled=false，daemon 退出（改配置后请重启 kylin oa-sync --daemon）")
            return
        if not bool(getattr(settings, "schedule_enabled", False)):
            logger.error("schedule_enabled=false，daemon 退出")
            return

        interval_hours = _interval_hours(settings)
        if not _RUNNING.acquire(blocking=False):
            logger.warning("上一轮 OA 同步仍在进行，跳过本轮")
        else:
            try:
                _run_one_tick(build_service)
            finally:
                _RUNNING.release()

        if _STOP.is_set():
            break
        logger.info("下一次 OA 同步将在 %s 小时后", interval_hours)
        if not _sleep_until_next(
            load_settings,
            sleep_fn=sleep_fn,
            chunk_seconds=chunk_seconds,
            planned_hours=interval_hours,
        ):
            return

    logger.info("OA 同步 daemon 已停止")


def _require_daemon_ready(settings: Any) -> None:
    if not bool(getattr(settings, "enabled", False)):
        raise ValueError("oa_settings.yaml 中 enabled=false，daemon 拒绝启动。请先启用同步。")
    if not bool(getattr(settings, "schedule_enabled", False)):
        raise ValueError(
            "oa_settings.yaml 中 schedule_enabled=false，daemon 拒绝启动。"
            "请设 schedule_enabled: true 后再执行 kylin oa-sync --daemon。"
        )
    _interval_hours(settings)


def _interval_hours(settings: Any) -> int:
    raw = getattr(settings, "interval_hours", 24)
    try:
        hours = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"interval_hours 必须是整数，当前值: {raw!r}") from error
    if hours < 1:
        raise ValueError(f"interval_hours 最小为 1，当前值: {hours}")
    return hours


def _run_one_tick(build_service: Callable[[], Any]) -> None:
    try:
        service = build_service()
        report = service.sync(full=False, dry_run=False, limit=0, only_ids=None)
        payload = report.to_dict() if hasattr(report, "to_dict") else report
        logger.info("OA 周期同步完成: %s", payload)
        failed = int(getattr(report, "failed", 0) or 0)
        if failed > 0:
            logger.warning("本轮 OA 同步有 %s 篇失败（详见 state last_error，可手动 --retry-failed）", failed)
    except Exception:
        logger.exception("本轮 OA 同步异常，将按间隔继续下一轮")


def _sleep_until_next(
    load_settings: Callable[[], Any],
    *,
    sleep_fn: Callable[[float], None],
    chunk_seconds: int,
    planned_hours: int | None = None,
) -> bool:
    """分段 sleep；期间若 enabled/schedule 被关掉则返回 False 表示应退出。"""
    if planned_hours is None:
        planned_hours = _interval_hours(load_settings())
    remaining = float(planned_hours * 3600)
    chunk = max(1, int(chunk_seconds))
    while remaining > 0 and not _STOP.is_set():
        settings = load_settings()
        if not bool(getattr(settings, "enabled", False)):
            logger.error("等待期间 enabled=false，daemon 退出")
            return False
        if not bool(getattr(settings, "schedule_enabled", False)):
            logger.error("等待期间 schedule_enabled=false，daemon 退出")
            return False
        step = min(float(chunk), remaining)
        sleep_fn(step)
        remaining -= step
    return not _STOP.is_set()
