"""Langfuse Trace 适配器：手动 observation 嵌套，兼容 SSE 异步生成器."""

from __future__ import annotations

import logging
import random
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from kylin.platform.observability.noop import (
    NoOpObservationHandle,
    NoOpTraceAdapter,
)

logger = logging.getLogger(__name__)

_current_observation: ContextVar[Any] = ContextVar(
    "kylin_trace_observation",
    default=None,
)


class LangfuseObservationHandle:
    """包装 Langfuse span/generation，失败全部吞掉."""

    def __init__(self, raw: Any):
        self._raw = raw

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
        try:
            kwargs: dict[str, Any] = {}
            if input is not None:
                kwargs["input"] = input
            if output is not None:
                kwargs["output"] = output
            if metadata is not None:
                kwargs["metadata"] = dict(metadata)
            if level is not None:
                kwargs["level"] = level
            if status_message is not None:
                kwargs["status_message"] = status_message
            if model is not None:
                kwargs["model"] = model
            if usage_details is not None:
                kwargs["usage_details"] = dict(usage_details)
            if kwargs:
                self._raw.update(**kwargs)
        except Exception as error:  # noqa: BLE001
            logger.debug("langfuse observation.update 失败: %s", error)

    def end(self) -> None:
        try:
            self._raw.end()
        except Exception as error:  # noqa: BLE001
            logger.debug("langfuse observation.end 失败: %s", error)

    def start_child(
        self,
        name: str,
        *,
        as_type: str = "span",
        input: Any = None,
        metadata: Mapping[str, Any] | None = None,
        model: str | None = None,
    ) -> LangfuseObservationHandle:
        kwargs: dict[str, Any] = {"name": name, "as_type": as_type}
        if input is not None:
            kwargs["input"] = input
        if metadata is not None:
            kwargs["metadata"] = dict(metadata)
        if model is not None:
            kwargs["model"] = model
        child = self._raw.start_observation(**kwargs)
        return LangfuseObservationHandle(child)


class KylinLangfuseTraceAdapter:
    """通过 Langfuse Python SDK v4 上报问答全链路."""

    def __init__(
        self,
        client: Any,
        *,
        sample_rate: float = 1.0,
        capture_content: bool = True,
    ):
        self._client = client
        self._sample_rate = max(0.0, min(1.0, float(sample_rate)))
        self._capture_content = bool(capture_content)

    @property
    def capture_content(self) -> bool:
        return self._capture_content

    def enabled(self) -> bool:
        return self._client is not None

    def current(self) -> Any:
        return _current_observation.get()

    def start_span(
        self,
        name: str,
        *,
        as_type: str = "span",
        input: Any = None,
        metadata: Mapping[str, Any] | None = None,
        model: str | None = None,
        parent: Any = None,
    ) -> Any:
        if not self.enabled():
            return NoOpObservationHandle()
        try:
            owner = parent if parent is not None else self.current()
            if isinstance(owner, NoOpObservationHandle):
                return NoOpObservationHandle()
            if isinstance(owner, LangfuseObservationHandle):
                return owner.start_child(
                    name,
                    as_type=as_type,
                    input=input,
                    metadata=metadata,
                    model=model,
                )
            if self._sample_rate <= 0.0 or (
                self._sample_rate < 1.0 and random.random() >= self._sample_rate
            ):
                return NoOpObservationHandle()
            kwargs: dict[str, Any] = {"name": name, "as_type": as_type}
            if input is not None:
                kwargs["input"] = input
            if metadata is not None:
                kwargs["metadata"] = dict(metadata)
            if model is not None:
                kwargs["model"] = model
            raw = self._client.start_observation(**kwargs)
            return LangfuseObservationHandle(raw)
        except Exception as error:  # noqa: BLE001
            logger.warning("langfuse start_span(%s) 失败: %s", name, error)
            return NoOpObservationHandle()

    @contextmanager
    def activate(
        self,
        observation: Any,
    ) -> Iterator[Any]:
        token: Token | None = None
        try:
            token = _current_observation.set(observation)
            yield observation
        finally:
            if token is not None:
                try:
                    _current_observation.reset(token)
                except ValueError:
                    # ContextVar.reset 的语义要求 token 必须在当前 context 中创建。
                    # SSE / 流式问答的 async generator 被外部 athrow/aclose 关闭时，
                    # finally 在关闭方的 context 里执行，token 必然跨 context——
                    # 原 context 已终结，无需也无法恢复，静默跳过是唯一正确处理。
                    pass

    @contextmanager
    def bind_trace(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        tags: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Iterator[None]:
        """绑定 trace 级属性；进入时一次性设置，退出阶段零操作.

        生命周期设计（适配异步生成器）：

        - 所有降级 / 兜底都发生在 ``yield`` 之前——进入阶段失败可以安全放弃；
        - ``yield`` 本身不包任何 try/except：@contextmanager 收到 ``throw()``
          后再 ``yield`` 会触发 ``generator didn't stop after throw()``；
        - 不在 finally 里 reset：属性经由 langfuse SDK 的 OTel context
          attach 注入，一次性动作；asyncio 每个请求 Task 持有独立 context，
          不回滚也不会跨请求残留。
        """
        if not self.enabled():
            yield None
            return
        try:
            from langfuse import propagate_attributes
        except Exception as error:  # noqa: BLE001
            logger.debug("langfuse propagate_attributes 不可用: %s", error)
            yield None
            return
        kwargs: dict[str, Any] = {}
        if user_id:
            kwargs["user_id"] = user_id
        if session_id:
            kwargs["session_id"] = session_id
        if tags:
            kwargs["tags"] = list(tags)
        if metadata:
            kwargs["metadata"] = dict(metadata)
        # 手动 __enter__ 只做 OTel context attach（一次性），token 丢弃即"不回滚"。
        # 参数集不兼容（TypeError）与 SDK 异常都在进入阶段降级，退出阶段保持干净。
        try:
            propagate_attributes(**kwargs).__enter__()
        except TypeError:
            # 旧参数集不兼容时降级为空绑定
            logger.debug("langfuse propagate_attributes 参数不兼容，降级为空绑定")
        except Exception as error:  # noqa: BLE001
            logger.debug("langfuse bind_trace 失败: %s", error)
        yield None

    def flush(self) -> None:
        if self._client is None:
            return
        try:
            self._client.flush()
        except Exception as error:  # noqa: BLE001
            logger.debug("langfuse flush 失败: %s", error)


def build_trace_port() -> NoOpTraceAdapter | KylinLangfuseTraceAdapter:
    """按 langfuse_settings 构造 TracePort；未启用或初始化失败返回 NoOp."""
    try:
        from kylin.platform.config.settings import Settings

        cfg = Settings.langfuse_settings
    except Exception as error:  # noqa: BLE001
        logger.warning("读取 langfuse_settings 失败，使用 NoOp: %s", error)
        return NoOpTraceAdapter()

    if not getattr(cfg, "enabled", False):
        return NoOpTraceAdapter()

    public_key = (getattr(cfg, "public_key", None) or "").strip()
    secret_key = (getattr(cfg, "secret_key", None) or "").strip()
    host = (getattr(cfg, "host", None) or "").strip()
    if not public_key or not secret_key:
        logger.warning("langfuse.enabled=true 但缺少 public_key/secret_key，使用 NoOp")
        return NoOpTraceAdapter()

    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host or None,
        )
        return KylinLangfuseTraceAdapter(
            client,
            sample_rate=float(getattr(cfg, "sample_rate", 1.0) or 1.0),
            capture_content=bool(getattr(cfg, "capture_content", True)),
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("初始化 Langfuse 客户端失败，使用 NoOp: %s", error)
        return NoOpTraceAdapter()
