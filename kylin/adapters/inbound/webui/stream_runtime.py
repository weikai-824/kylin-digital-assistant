"""问答流式运行时：后台线程消费 SSE，主线程按帧渲染快照。

为什么需要它
------------
Streamlit 的脚本模型是「一次 script run 同步跑完，才把结果推给浏览器」。
原先 ``kb_chat`` 在脚本内直接 ``for ... in client.chat.completions.create(...)``，
整段等待期间浏览器收不到任何更新：用户发出的问题、后端已经 yield 的
progress 阶段、已经到达的答案 token，全部积压到脚本结束才一次性显示，
连「正在检索」这种提示都形同虚设。

本模块把 SSE 消费挪进后台线程，主线程只做「读快照 → 渲染 → 等一小会儿
→ 重跑」，于是等待过程第一次变得可见：阶段进度会走，答案会逐字流出。

线程边界（重要）
----------------
后台线程**不碰** ``st.session_state``：Streamlit 会话状态不是线程安全的。
后台线程只写本模块的 handle（自带锁），主线程负责读取并渲染。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 阶段顺序与文案：与 ports/application/knowledge_qa.py 的 progress 常量对齐
STAGE_ORDER: tuple[str, ...] = ("planning", "retrieving", "generating")
STAGE_LABELS: dict[str, str] = {
    "planning": "理解问题",
    "retrieving": "检索知识库",
    "generating": "组织回答",
}

# 主线程渲染帧间隔（秒）。太小会把 CPU 耗在重跑全页上，太大则不够顺滑。
REFRESH_INTERVAL: float = 0.15
# 单次问答的等待上限，超时即放弃等待（后端自身还有 HTTPX_DEFAULT_TIMEOUT）。
# 与 oa-console 前端 STREAM_TIMEOUTS.total（120s）+ 服务端 SSE 上限 125s 对齐，
# 避免同一条问答在不同入口的超时口径不一致（原值 300s 会让前端早已放弃的问答继续空跑）。
MAX_STREAM_SECONDS: float = 125.0
# handle 在注册表里的存活时间，防止异常线程泄漏
HANDLE_TTL_SECONDS: float = 600.0


def chunk_extra(chunk: object, name: str) -> Any:
    """读取本服务挂在 chunk 上的扩展字段（progress / docs / session_id 等）。"""
    value = getattr(chunk, name, None)
    if value is not None:
        return value
    extras = getattr(chunk, "model_extra", None) or {}
    if isinstance(extras, dict) and name in extras:
        return extras.get(name)
    return None


def chunk_text(chunk: object) -> str:
    """从流式 chunk 取增量文本；兼容 OpenAI choices 与本服务顶层 content。"""
    choices = getattr(chunk, "choices", None)
    if choices:
        delta = getattr(choices[0], "delta", None)
        if delta is not None:
            return getattr(delta, "content", None) or ""
    return getattr(chunk, "content", None) or ""


@dataclass
class StreamSnapshot:
    """某一时刻的流式状态快照（主线程只读的不可变视图）。"""

    run_id: str = ""
    stage: str = ""
    stages_done: tuple[str, ...] = ()
    text: str = ""
    docs_ready: bool = False
    docs: tuple[str, ...] = ()
    raw_docs: tuple[dict, ...] = ()
    ticket_id: Any = None
    session_id: Optional[str] = None
    error: str = ""
    done: bool = False
    interrupted: bool = False
    elapsed: float = 0.0


class StreamHandle:
    """一次问答的流式状态容器；后台线程写，主线程读。"""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._started_at = time.time()
        self._finished_at: Optional[float] = None
        self._parts: list[str] = []
        self._stages_done: list[str] = []
        self.stage = ""
        self.docs_ready = False
        self.docs: list[str] = []
        self.raw_docs: list[dict] = []
        self.ticket_id: Any = None
        self.session_id: Optional[str] = None
        self.error = ""

    # ---- 后台线程侧 ----

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def set_stage(self, stage: str) -> None:
        with self._lock:
            if stage not in self._stages_done:
                self._stages_done.append(stage)
            self.stage = stage

    def append_text(self, piece: str) -> None:
        if not piece:
            return
        with self._lock:
            self._parts.append(piece)

    def set_meta(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def fail(self, message: str) -> None:
        with self._lock:
            self.error = message

    def finish(self) -> None:
        self._finished_at = self._finished_at or time.time()

    # ---- 主线程侧 ----

    @property
    def finished_at(self) -> Optional[float]:
        return self._finished_at

    def snapshot(self) -> StreamSnapshot:
        with self._lock:
            stages = tuple(self._stages_done)
            text = "".join(self._parts)
            return StreamSnapshot(
                run_id=self.run_id,
                stage=self.stage,
                stages_done=stages,
                text=text,
                docs_ready=self.docs_ready,
                docs=tuple(self.docs),
                raw_docs=tuple(self.raw_docs),
                ticket_id=self.ticket_id,
                session_id=self.session_id,
                error=self.error,
                done=self._finished_at is not None,
                interrupted=self._stop.is_set(),
                elapsed=time.time() - self._started_at,
            )


def _consume(
    handle: StreamHandle,
    *,
    api_url: str,
    kb_name: str,
    model: str,
    messages: Sequence[dict],
    extra_body: dict,
) -> None:
    """后台线程主体：消费 SSE 并写入 handle。任何异常都收敛成 error。"""
    import openai

    started_at = time.time()
    try:
        client = openai.Client(
            base_url=f"{api_url}/knowledge_base/local_kb/{kb_name}",
            api_key="NONE",
        )
        for chunk in client.chat.completions.create(
            messages=list(messages),
            model=model,
            stream=True,
            extra_body=extra_body,
        ):
            if handle.stopped:
                logger.info("问答流式被用户中断 run=%s", handle.run_id)
                break

            progress = chunk_extra(chunk, "progress")
            if isinstance(progress, str) and progress.strip() in STAGE_ORDER:
                handle.set_stage(progress.strip())

            session_id = chunk_extra(chunk, "session_id")
            if session_id:
                handle.set_meta(session_id=session_id)

            docs = chunk_extra(chunk, "docs")
            raw_docs = chunk_extra(chunk, "raw_docs")
            if docs is not None or raw_docs is not None:
                handle.set_meta(
                    docs_ready=True,
                    docs=list(docs or []),
                    raw_docs=list(raw_docs) if isinstance(raw_docs, list) else [],
                    ticket_id=chunk_extra(chunk, "ticket_id"),
                )

            handle.append_text(chunk_text(chunk))

            # OpenAI 流约定：choices[0].finish_reason 非空即本轮可见输出结束，
            # 不必再等连接对端把收尾任务做完才退出循环。
            finish = None
            choices = getattr(chunk, "choices", None)
            if choices:
                finish = getattr(choices[0], "finish_reason", None)
            if finish is None:
                finish = chunk_extra(chunk, "finish_reason")
            if finish:
                break

            if time.time() - started_at > MAX_STREAM_SECONDS:
                handle.fail("生成超时，已停止等待。")
                break
    except Exception as error:  # noqa: BLE001 - 边界层统一兜底，避免线程静默死亡
        body = getattr(error, "body", None) or str(error)
        handle.fail(str(body))
        logger.warning("问答流式失败 run=%s: %s", handle.run_id, error)
    finally:
        handle.finish()


_handles: dict[str, StreamHandle] = {}
_handles_lock = threading.Lock()


def _reap_locked() -> None:
    """清理已结束 / 超时的 handle（调用方需持有 _handles_lock）。"""
    now = time.time()
    expired = [
        run_id
        for run_id, handle in _handles.items()
        if (handle.finished_at or 0) + HANDLE_TTL_SECONDS < now
    ]
    for run_id in expired:
        _handles.pop(run_id, None)


def start(
    *,
    api_url: str,
    kb_name: str,
    model: str,
    messages: Sequence[dict],
    extra_body: dict,
) -> str:
    """启动一次后台流式问答，返回 run_id。"""
    run_id = uuid.uuid4().hex
    handle = StreamHandle(run_id)
    with _handles_lock:
        _reap_locked()
        _handles[run_id] = handle
    worker = threading.Thread(
        target=_consume,
        kwargs={
            "handle": handle,
            "api_url": api_url,
            "kb_name": kb_name,
            "model": model,
            "messages": list(messages),
            "extra_body": dict(extra_body),
        },
        name=f"qa-stream-{run_id[:8]}",
        daemon=True,
    )
    worker.start()
    return run_id


def peek(run_id: str) -> Optional[StreamSnapshot]:
    """读取快照；run_id 不存在时返回 None。"""
    with _handles_lock:
        handle = _handles.get(run_id)
    return handle.snapshot() if handle else None


def stop(run_id: str) -> None:
    """请求中断；线程在下一次取到 chunk 时退出。"""
    with _handles_lock:
        handle = _handles.get(run_id)
    if handle is not None:
        handle.request_stop()


def drop(run_id: str) -> None:
    """流结束后从注册表移除。"""
    with _handles_lock:
        handle = _handles.pop(run_id, None)
    if handle is not None:
        handle.request_stop()
