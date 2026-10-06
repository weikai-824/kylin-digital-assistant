"""空实现 Trace：观测关闭或后端不可用时保证主链路零影响."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any


class NoOpObservationHandle:
    def update(
        self,
        *,
        input: Any = None,
        output: Any = None,
        metadata: Mapping[str, Any] | None = None,
        level: str | None = None,
        status_message: str | None = None,
        model: str | None = None,
        usage_details: Mapping[str, int] | None = None,
    ) -> None:
        return None

    def end(self) -> None:
        return None


class NoOpTraceAdapter:
    """始终 enabled=False；所有方法为无操作."""

    def enabled(self) -> bool:
        return False

    def current(self) -> NoOpObservationHandle | None:
        return None

    def start_span(
        self,
        name: str,
        *,
        as_type: str = "span",
        input: Any = None,
        metadata: Mapping[str, Any] | None = None,
        model: str | None = None,
        parent: NoOpObservationHandle | None = None,
    ) -> NoOpObservationHandle:
        return NoOpObservationHandle()

    @contextmanager
    def activate(
        self,
        observation: NoOpObservationHandle,
    ) -> Iterator[NoOpObservationHandle]:
        yield observation

    @contextmanager
    def bind_trace(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        tags: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Iterator[None]:
        yield None

    def flush(self) -> None:
        return None
