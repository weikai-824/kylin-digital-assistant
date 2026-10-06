"""OA 周期调度单元测试（不连真实 OA）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kylin.applications.knowledge_ingestion.oa_schedule import (
    _interval_hours,
    _require_daemon_ready,
    request_stop,
    run_oa_sync_schedule,
)


def test_interval_hours_rejects_below_one():
    with pytest.raises(ValueError, match="最小为 1"):
        _interval_hours(SimpleNamespace(interval_hours=0))


def test_daemon_requires_enabled_and_schedule():
    with pytest.raises(ValueError, match="enabled=false"):
        _require_daemon_ready(SimpleNamespace(enabled=False, schedule_enabled=True, interval_hours=24))
    with pytest.raises(ValueError, match="schedule_enabled=false"):
        _require_daemon_ready(SimpleNamespace(enabled=True, schedule_enabled=False, interval_hours=24))


def test_run_schedule_one_tick_then_stop(monkeypatch):
    calls = {"sync": 0, "builds": 0}
    state = {
        "enabled": True,
        "schedule_enabled": True,
        "interval_hours": 1,
        "run_on_start": True,
    }

    def load_settings():
        return SimpleNamespace(**state)

    class FakeReport:
        failed = 0

        def to_dict(self):
            return {"failed": 0, "ingested": 1}

    class FakeService:
        def sync(self, **_kwargs):
            calls["sync"] += 1
            request_stop()
            return FakeReport()

    def build_service():
        calls["builds"] += 1
        return FakeService()

    sleeps: list[float] = []

    def sleep_fn(seconds: float) -> None:
        sleeps.append(seconds)
        # 不应在 stop 后还需要长睡；若进入 sleep 则立刻再 stop
        request_stop()

    run_oa_sync_schedule(
        load_settings=load_settings,
        build_service=build_service,
        sleep_fn=sleep_fn,
        chunk_seconds=1,
    )
    assert calls["sync"] == 1
    assert calls["builds"] == 1
