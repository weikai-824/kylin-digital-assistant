"""将 Chat 模型端口适配到现有 Kylin 模型工厂."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Optional

import openai

from kylin.adapters.outbound.model.clients import (
    DISABLE_THINKING_EXTRA_BODY,
    get_ChatOpenAI,
    get_default_llm,
    get_model_info,
)
from kylin.platform.config.settings import Settings
from kylin.platform.observability.logging import build_logger
from kylin.platform.observability.noop import NoOpTraceAdapter
from kylin.ports.model.chat import ChatMessage, ChatModelUnavailableError
from kylin.ports.observability.trace import TracePort

logger = build_logger()


def _content_as_text(response: object) -> str:
    raw = getattr(response, "content", None)
    if raw is None:
        return str(response)
    if not isinstance(raw, list):
        return str(raw)

    parts: list[str] = []
    for block in raw:
        if isinstance(block, dict):
            text = block.get("text") or block.get("content") or ""
        else:
            text = getattr(block, "text", None) or block
        if text:
            parts.append(str(text))
    return "\n".join(parts)


def _model_chain(model: Optional[str]) -> list[str]:
    """构造模型尝试链：主模型 → 备用模型（去重，备用须已配置）。"""
    primary = model or get_default_llm()
    chain = [primary]
    fallback = Settings.model_settings.FALLBACK_LLM_MODEL
    if fallback and fallback != primary:
        chain.append(fallback)
    return chain


def _extra_body_for(model_name: str) -> Optional[dict]:
    """按平台适配"关闭思考"参数（仅当平台配置 disable_thinking=True）：

    - vLLM / Qwen 系（oa-llm 等内网网关）：chat_template_kwargs.enable_thinking=false
    - DeepSeek 官方 API：thinking.type=disabled —— 它不识别 vLLM 参数，传了会 400；
      且 DeepSeek 思考模式默认开启，思考会吃掉 max_tokens 预算导致 content 为空。
    """
    info = get_model_info(model_name)
    if not info or not info.get("disable_thinking"):
        return None
    base_url = (info.get("api_base_url") or "").lower()
    if "deepseek" in base_url:
        return {"thinking": {"type": "disabled"}}
    return dict(DISABLE_THINKING_EXTRA_BODY)


def _is_retryable_error(exc: Exception) -> bool:
    """是否可降级重试：400 属参数/请求问题，降级无意义且会掩盖 bug。

    429 / 5xx / 连接失败 / 超时 / 端口级不可用 → 可降级到备用模型。
    """
    if isinstance(exc, openai.BadRequestError):
        return False
    if isinstance(exc, openai.OpenAIError):
        return True
    if isinstance(exc, ChatModelUnavailableError):
        return True
    if isinstance(exc, TimeoutError):
        return True
    return False


class KylinChatModelAdapter:
    """通过模型客户端实现 ``ChatModelPort``."""

    def __init__(self, trace: TracePort | None = None):
        self._trace = trace if trace is not None else NoOpTraceAdapter()

    def default_model(self) -> str:
        """返回当前配置的默认模型名."""
        return get_default_llm()

    def grounding_style(self, model_name: str | None = None) -> str:
        """返回模型所属平台的接地判定风格，供问答应用选择 prompt 模板.

        strict：严格措辞匹配（默认，QwQ 等）；lenient：参考资料含直接相关信息即须回答（DeepSeek 等易过度拒答模型）。
        """
        info = get_model_info(model_name or get_default_llm())
        return (info or {}).get("grounding_style") or "strict"

    @staticmethod
    def _create_llm(
        *,
        model: Optional[str],
        temperature: float,
        max_tokens: Optional[int],
        streaming: bool,
    ):
        model_name = model or get_default_llm()
        return get_ChatOpenAI(
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            streaming=streaming,
            callbacks=[],
            verbose=False,
            extra_body=_extra_body_for(model_name),
        )

    def _generation_input(self, messages: Sequence[ChatMessage]) -> Any:
        capture = bool(getattr(Settings.langfuse_settings, "capture_content", True))
        if capture:
            return [dict(message) for message in messages]
        return {"message_count": len(messages)}

    def _start_generation(self, *, name: str, model: Optional[str], messages: Sequence[ChatMessage]):
        parent = self._trace.current()
        return self._trace.start_span(
            name,
            as_type="generation",
            model=model or get_default_llm(),
            input=self._generation_input(messages),
            parent=parent,
        )

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Optional[str]:
        chain = _model_chain(model)
        last_err: Optional[Exception] = None
        for i, name in enumerate(chain):
            gen = self._start_generation(name="chat_complete", model=name, messages=messages)
            with self._trace.activate(gen):
                llm = self._create_llm(
                    model=name,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    streaming=False,
                )
                if llm is None:
                    last_err = ChatModelUnavailableError(name)
                    logger.warning(f"LLM {name} 客户端创建失败，{'尝试备用模型' if i < len(chain) - 1 else '无可用模型'}")
                    gen.update(level="ERROR", status_message="client_create_failed")
                    gen.end()
                    continue
                try:
                    response = llm.invoke([dict(message) for message in messages])
                    text = _content_as_text(response)
                    if i > 0:
                        logger.warning(f"LLM 自动降级: {chain[0]} -> {name}")
                        gen.update(metadata={"fallback_from": chain[0]})
                    gen.update(output=text if getattr(Settings.langfuse_settings, "capture_content", True) else {"chars": len(text)})
                    gen.end()
                    return text
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    gen.update(level="ERROR", status_message=str(e))
                    gen.end()
                    if not _is_retryable_error(e) or i >= len(chain) - 1:
                        break
                    logger.warning(f"LLM {name} 调用失败（{type(e).__name__}: {e}），降级到备用模型")
        if last_err is not None:
            logger.error(f"LLM 全部不可用（尝试链 {chain}）: {last_err}")
        return None

    async def complete_async(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = 1024,
    ) -> str:
        """异步完成一次生成；主模型与备用模型均不可用时抛出端口级错误。"""
        chain = _model_chain(model)
        last_err: Optional[Exception] = None
        for i, name in enumerate(chain):
            gen = self._start_generation(name="chat_complete_async", model=name, messages=messages)
            with self._trace.activate(gen):
                llm = self._create_llm(
                    model=name,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    streaming=False,
                )
                if llm is None:
                    last_err = ChatModelUnavailableError(name)
                    logger.warning(f"LLM {name} 客户端创建失败，{'尝试备用模型' if i < len(chain) - 1 else '无可用模型'}")
                    gen.update(level="ERROR", status_message="client_create_failed")
                    gen.end()
                    continue
                try:
                    response = await llm.ainvoke([dict(message) for message in messages])
                    text = _content_as_text(response)
                    if i > 0:
                        logger.warning(f"LLM 自动降级: {chain[0]} -> {name}")
                        gen.update(metadata={"fallback_from": chain[0]})
                    gen.update(output=text if getattr(Settings.langfuse_settings, "capture_content", True) else {"chars": len(text)})
                    gen.end()
                    return text
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    gen.update(level="ERROR", status_message=str(e))
                    gen.end()
                    if not _is_retryable_error(e) or i >= len(chain) - 1:
                        break
                    logger.warning(f"LLM {name} 调用失败（{type(e).__name__}: {e}），降级到备用模型")
        raise ChatModelUnavailableError(chain[0]) from last_err

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = 1024,
    ) -> AsyncIterator[str]:
        """以文本 token 流形式返回异步生成结果；首 token 前失败可降级备用模型。"""
        chain = _model_chain(model)
        last_err: Optional[Exception] = None
        for i, name in enumerate(chain):
            gen = self._start_generation(name="chat_stream", model=name, messages=messages)
            with self._trace.activate(gen):
                llm = self._create_llm(
                    model=name,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    streaming=True,
                )
                if llm is None:
                    last_err = ChatModelUnavailableError(name)
                    logger.warning(f"LLM {name} 客户端创建失败，{'尝试备用模型' if i < len(chain) - 1 else '无可用模型'}")
                    gen.update(level="ERROR", status_message="client_create_failed")
                    gen.end()
                    continue
                emitted = False
                parts: list[str] = []
                try:
                    async for chunk in llm.astream([dict(message) for message in messages]):
                        text = _content_as_text(chunk)
                        emitted = True
                        parts.append(text)
                        yield text
                    if i > 0:
                        logger.warning(f"LLM 自动降级: {chain[0]} -> {name}")
                        gen.update(metadata={"fallback_from": chain[0]})
                    full = "".join(parts)
                    gen.update(
                        output=full
                        if getattr(Settings.langfuse_settings, "capture_content", True)
                        else {"chars": len(full)}
                    )
                    return
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    gen.update(level="ERROR", status_message=str(e))
                    if emitted or not _is_retryable_error(e) or i >= len(chain) - 1:
                        raise
                    logger.warning(f"LLM {name} 流式启动失败（{type(e).__name__}: {e}），降级到备用模型")
                finally:
                    gen.end()
        raise ChatModelUnavailableError(chain[0]) from last_err
