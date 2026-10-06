"""空实现 Trace 适配器（实现位于 platform，避免应用层依赖 outbound）."""

from kylin.platform.observability.noop import NoOpObservationHandle, NoOpTraceAdapter

__all__ = ["NoOpObservationHandle", "NoOpTraceAdapter"]
