"""全链路 Trace 端口：问答编排与 LLM 调用共用，不绑定具体观测后端."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol


class ObservationHandle(Protocol):
    """一次 span / generation；须显式 ``end()``（适配异步生成器跨 yield）."""

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
    ) -> None: ...

    def end(self) -> None: ...


class TracePort(Protocol):
    """应用层只依赖本端口；Langfuse / NoOp 由适配器实现."""

    def enabled(self) -> bool:
        """是否实际上报（关闭时编排仍可调用，但应为廉价 no-op）."""

    def current(self) -> ObservationHandle | None:
        """当前上下文中的 observation（供 LLM 适配器挂子 generation）."""

    def start_span(
        self,
        name: str,
        *,
        as_type: str = "span",
        input: Any = None,
        metadata: Mapping[str, Any] | None = None,
        model: str | None = None,
        parent: ObservationHandle | None = None,
    ) -> ObservationHandle:
        """创建 span。``parent`` 缺省时挂到 ``current()``；皆无则开新 root."""

    def activate(
        self,
        observation: ObservationHandle,
    ) -> AbstractContextManager[ObservationHandle]:
        """将 observation 设为 current；退出时尽力恢复（跨 context 关闭时静默跳过），不负责 end()."""

    def bind_trace(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        tags: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AbstractContextManager[None]:
        """绑定 trace 级属性（用户 / 会话 / 标签）；进入时一次性设置，退出不回滚.

        适配异步生成器被外部 athrow/aclose 的场景：实现不得在退出阶段做
        ContextVar reset（token 必然跨 context），也不得在异常处理路径再 yield。
        """

    def flush(self) -> None:
        """尽力刷出缓冲（失败须吞掉）."""
