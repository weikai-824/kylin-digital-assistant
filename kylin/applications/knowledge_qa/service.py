"""知识问答应用服务：计划、检索、接地生成与会话记忆的唯一编排入口."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from kylin.applications.knowledge_qa.context import assemble_context
from kylin.applications.knowledge_qa.grounding import (
    REFUSAL_NO_EVIDENCE,
    ThinkStreamFilter,
    is_model_refusal,
    sanitize_citations,
    strip_think,
)
from kylin.applications.knowledge_qa.prompt import (
    CHITCHAT_USER,
    RECALL_USER,
    grounded_user,
)
from kylin.applications.knowledge_qa.query_plan import QueryPlanner
from kylin.applications.knowledge_qa.retrieval import KnowledgeRetriever
from kylin.applications.long_term_memory.extractor import MemoryExtractor
from kylin.platform.config.settings import Settings
from kylin.ports.application.knowledge_gap import KnowledgeGapPort
from kylin.ports.application.knowledge_qa import (
    PROGRESS_GENERATING,
    PROGRESS_PLANNING,
    PROGRESS_RETRIEVING,
    KnowledgeQaEvent,
    KnowledgeQaRequest,
)
from kylin.ports.data.conversation_memory import (
    ConversationMemoryPort,
)
from kylin.ports.data.knowledge_search import KnowledgeSearchPort
from kylin.ports.data.long_term_memory import LongTermMemoryPort
from kylin.ports.model.chat import ChatMessage, ChatModelPort
from kylin.ports.observability.trace import TracePort
from kylin.platform.observability.noop import NoOpTraceAdapter

logger = logging.getLogger(__name__)


async def _cancel_task(task: "asyncio.Task | None") -> None:
    """取消未完成的并行任务；已结束或不存在则无操作。"""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _spawn_background(coro) -> None:
    """把收尾协程丢进当前事件循环，失败只打日志，不挡 SSE 关闭。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("无运行中的事件循环，跳过后台收尾任务")
        return

    task = loop.create_task(coro)

    def _log_done(done: asyncio.Task) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            return
        except Exception as error:  # noqa: BLE001
            logger.warning("后台收尾任务失败: %s", error)

    task.add_done_callback(_log_done)


def _trace_input_text(text: str, *, capture: bool, limit: int = 2000) -> dict:
    if capture:
        return {"text": text[:limit]}
    return {"chars": len(text or "")}


def _doc_summary(documents: list, *, limit: int = 8) -> list[dict]:
    rows: list[dict] = []
    for doc in documents[:limit]:
        meta = getattr(doc, "metadata", None) or {}
        rows.append(
            {
                "score": getattr(doc, "score", None),
                "source": meta.get("source") or meta.get("filename"),
            }
        )
    return rows


class KnowledgeQaService:
    """不依赖 FastAPI/SSE 的知识问答主链路."""

    def __init__(
        self,
        *,
        planner: QueryPlanner,
        retriever: KnowledgeRetriever,
        search_port: KnowledgeSearchPort,
        chat_model: ChatModelPort,
        memory: ConversationMemoryPort,
        knowledge_gap: KnowledgeGapPort | None = None,
        long_term_memory: LongTermMemoryPort | None = None,
        memory_extractor: MemoryExtractor | None = None,
        trace: TracePort | None = None,
    ):
        self._planner = planner
        self._retriever = retriever
        self._search_port = search_port
        self._chat_model = chat_model
        self._memory = memory
        self._knowledge_gap = knowledge_gap
        self._long_term_memory = long_term_memory
        # 未显式注入时用主链路的 chat_model 兜底，保证提取器始终有模型可用
        self._memory_extractor = memory_extractor or MemoryExtractor(chat_model=chat_model)
        self._trace = trace if trace is not None else NoOpTraceAdapter()

    def knowledge_base_exists(self, knowledge_base_name: str) -> bool:
        """供 HTTP 边界层将不存在的知识库映射为 404."""
        return self._search_port.exists(knowledge_base_name)

    def _progress(
        self,
        *,
        session_id: str,
        model: str | None,
        stage: str,
    ) -> KnowledgeQaEvent:
        return KnowledgeQaEvent(
            kind="progress",
            session_id=session_id,
            model=model,
            content=stage,
        )

    def _thought(
        self,
        *,
        session_id: str,
        model: str | None,
        stage: str,
        content: str,
    ) -> KnowledgeQaEvent:
        """结构化思考中间产物事件（虚化思维链内容）。"""
        return KnowledgeQaEvent(
            kind="thought",
            session_id=session_id,
            model=model,
            stage=stage,
            content=content,
        )

    async def _recall_long_term_memory(self, request: KnowledgeQaRequest) -> str:
        """召回该用户长期记忆，拼成注入用的 system 文案；未启用 / 失败返回空串。

        读取侧挂载点：生成前调用。任何异常只记日志，不让记忆故障拖垮问答。
        """
        if not request.user_id or self._long_term_memory is None:
            return ""
        try:
            recalled = await asyncio.to_thread(
                self._long_term_memory.recall,
                request.user_id,
                request.query,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("长期记忆召回失败 user=%s: %s", request.user_id, error)
            return ""
        inject_max = Settings.memory_settings.inject_max_items
        inject_chars = Settings.memory_settings.inject_max_chars
        items = [
            memory.content[:inject_chars]
            for memory in recalled[:inject_max]
            if (memory.content or "").strip()
        ]
        if not items:
            return ""
        return (
            "该用户的已知信息（跨会话长期记忆，回答与其身份、历史关注点相关时可参考）：\n"
            + "\n".join(f"- {item}" for item in items)
        )

    async def _write_long_term_memory(
        self,
        request: KnowledgeQaRequest,
        session_id: str,
        query: str,
        answer: str,
    ) -> None:
        """从本轮问答提取事实并写入长期记忆；任何异常只记日志，不影响主链路。

        写入侧挂载点：save_turn + maybe_summarize 之后调用（会话记忆先落库，再沉淀长期记忆）。
        """
        if not request.user_id or self._long_term_memory is None:
            logger.info(
                f"_write_long_term_memory 跳过 user_id={request.user_id!r} "
                f"long_term_memory={'None' if self._long_term_memory is None else 'OK'} "
                f"answer_len={len(answer or '')}"
            )
            return
        try:
            memories = await asyncio.to_thread(
                self._memory_extractor.extract,
                query,
                answer,
                user_id=request.user_id,
                session_id=session_id,
                kb_name=request.knowledge_base_name,
            )
            if memories:
                added = await asyncio.to_thread(
                    self._long_term_memory.remember,
                    request.user_id,
                    memories,
                    session_id=session_id,
                    kb_name=request.knowledge_base_name,
                )
                if added:
                    logger.info(
                        "长期记忆新增 %d 条 user=%s kb=%s",
                        added,
                        request.user_id,
                        request.knowledge_base_name,
                    )
        except Exception as error:  # noqa: BLE001
            logger.warning("长期记忆写入失败（不影响本次回答）: %s", error)

    async def _post_stream_finalize(
        self,
        request: KnowledgeQaRequest,
        session,
        session_id: str,
        answer: str,
    ) -> None:
        """流式可见输出结束后的慢收尾：摘要 + 长期记忆（不阻塞 SSE）。"""
        span = self._trace.start_span(
            "post_stream",
            as_type="span",
            input={"session_id": session_id, "answer_chars": len(answer or "")},
        )
        try:
            with self._trace.activate(span):
                sum_span = self._trace.start_span("summarize", as_type="span", parent=span)
                try:
                    await asyncio.to_thread(
                        self._memory.maybe_summarize,
                        session,
                        model=request.model or self._chat_model.default_model(),
                    )
                except Exception as error:  # noqa: BLE001
                    sum_span.update(level="ERROR", status_message=str(error))
                    logger.warning("会话摘要失败（不影响本次回答）: %s", error)
                finally:
                    sum_span.end()

                ltm_span = self._trace.start_span("ltm_write", as_type="span", parent=span)
                try:
                    await self._write_long_term_memory(
                        request, session_id, request.query, answer
                    )
                except Exception as error:  # noqa: BLE001
                    ltm_span.update(level="ERROR", status_message=str(error))
                    raise
                finally:
                    ltm_span.end()
        finally:
            span.end()

    async def run(self, request: KnowledgeQaRequest) -> AsyncIterator[KnowledgeQaEvent]:
        """运行一次问答并产出 progress/start/token/complete 事件."""
        capture = bool(getattr(Settings.langfuse_settings, "capture_content", True))
        tags = ["kb_chat", request.knowledge_base_name]
        root_input = {
            "query": _trace_input_text(request.query, capture=capture),
            "knowledge_base": request.knowledge_base_name,
            "model": request.model,
            "stream": request.stream,
        }
        with self._trace.bind_trace(
            user_id=request.user_id or None,
            session_id=request.session_id or None,
            tags=tags,
            metadata={"knowledge_base": request.knowledge_base_name},
        ):
            root = self._trace.start_span(
                "kb_chat",
                as_type="chain",
                input=root_input,
                metadata={
                    "knowledge_base": request.knowledge_base_name,
                    "stream": request.stream,
                },
            )
            with self._trace.activate(root):
                try:
                    async for event in self._run_inner(request, root=root, capture=capture):
                        yield event
                except Exception as error:
                    root.update(level="ERROR", status_message=str(error))
                    raise
                finally:
                    root.end()
                    # flush 可能数百 ms～数秒；放到后台，避免拖住 SSE 关闭与前端 done
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(asyncio.to_thread(self._trace.flush))
                    except RuntimeError:
                        self._trace.flush()

    async def _run_inner(
        self,
        request: KnowledgeQaRequest,
        *,
        root,
        capture: bool,
    ) -> AsyncIterator[KnowledgeQaEvent]:
        # ensure_ready（embed 可用性冷探）已下移到检索阶段前（progress(retrieving) 之后），
        # 保证 planning 阶段的 progress/thought 事件不被知识库就绪检查阻塞：
        # 即使 embed 端首次冷加载，前端也能第一时间看到"理解中"占位与改写意图，而非空白等待。
        session = self._memory.open(request.session_id, request.knowledge_base_name)
        session_id = session.session_id
        history = list(self._memory.load_history(session)) or list(request.history)
        root.update(metadata={"session_id": session_id})

        # 长期记忆召回与「查询规划 + 检索」并行启动（都是独立 IO，原先串行多等一次 RTT）
        recall_task = (
            asyncio.create_task(self._recall_long_term_memory(request))
            if request.user_id and self._long_term_memory is not None
            else None
        )

        if request.stream:
            yield self._progress(
                session_id=session_id,
                model=request.model,
                stage=PROGRESS_PLANNING,
            )

        plan_max_tokens = int(
            getattr(Settings.kb_settings, "QUERY_PLAN_MAX_TOKENS", 256) or 256
        )
        plan_span = self._trace.start_span(
            "planning",
            as_type="span",
            input=_trace_input_text(request.query, capture=capture),
            parent=root,
        )
        with self._trace.activate(plan_span):
            try:
                plan = await asyncio.to_thread(
                    self._planner.build,
                    request.query,
                    history,
                    model=request.model,
                    temperature=request.temperature,
                    max_tokens=plan_max_tokens,
                )
                plan_span.update(
                    output={
                        "need_retrieval": plan.need_retrieval,
                        "query_type": plan.query_type,
                        "standalone_query": plan.standalone_query
                        if capture
                        else bool(plan.standalone_query),
                        "sub_queries": plan.sub_queries if capture else len(plan.sub_queries or []),
                        "notes": plan.notes if capture else None,
                    }
                )
            except Exception as error:
                plan_span.update(level="ERROR", status_message=str(error))
                raise
            finally:
                plan_span.end()

        logger.info(
            "query_plan kb=%s need=%s type=%s standalone=%r subs=%s notes=%s",
            request.knowledge_base_name,
            plan.need_retrieval,
            plan.query_type,
            plan.standalone_query,
            plan.sub_queries,
            plan.notes,
        )

        # 结构化思考中间产物（planning）：把改写后的检索意图透给前端做虚化思维链
        if request.stream and plan.need_retrieval:
            planning_lines: list[str] = []
            if plan.standalone_query and plan.standalone_query.strip() != request.query.strip():
                planning_lines.append(f"改写检索意图：{plan.standalone_query}")
            subs = [s for s in (plan.sub_queries or []) if s and s.strip()]
            if subs:
                joined = "、".join(subs[:4])
                planning_lines.append(f"拆解出 {len(subs)} 个检索点：{joined}")
            if plan.query_type:
                planning_lines.append(f"判定问题类型：{plan.query_type}")
            for line in planning_lines:
                yield self._thought(
                    session_id=session_id,
                    model=request.model,
                    stage=PROGRESS_PLANNING,
                    content=line,
                )

        if request.stream:
            yield self._progress(
                session_id=session_id,
                model=request.model,
                stage=PROGRESS_RETRIEVING,
            )

        # 检索前确保知识库就绪（embed 模型可用性冷探；OK 结果有 600s TTL 缓存，命中即过）。
        # 放在 progress(retrieving) 之后：冷探耗时不再阻塞 planning 阶段 progress/thought 推送。
        ready_span = self._trace.start_span(
            "ensure_ready",
            as_type="span",
            input={"knowledge_base": request.knowledge_base_name},
            parent=root,
        )
        try:
            await asyncio.to_thread(
                self._search_port.ensure_ready,
                request.knowledge_base_name,
            )
        except Exception as error:
            ready_span.update(level="ERROR", status_message=str(error))
            raise
        finally:
            ready_span.end()

        ticket_kb = self._knowledge_gap.ticket_kb_name() if self._knowledge_gap else ""
        kb_names = self._retriever.knowledge_base_names(
            request.knowledge_base_name,
            ticket_kb,
        )
        retrieve_span = self._trace.start_span(
            "retrieving",
            as_type="retriever",
            input={
                "knowledge_bases": kb_names,
                "top_k": request.top_k,
                "score_threshold": request.score_threshold,
                "need_retrieval": plan.need_retrieval,
            },
            parent=root,
        )
        with self._trace.activate(retrieve_span):
            try:
                documents = await asyncio.to_thread(
                    self._retriever.search_across_kbs,
                    plan,
                    knowledge_base_names=kb_names,
                    top_k=request.top_k,
                    score_threshold=request.score_threshold,
                    dept=request.dept,
                    dept_exempt=request.dept_exempt,
                )
                scores = [float(getattr(doc, "score", 0) or 0) for doc in documents]
                retrieve_span.update(
                    output={
                        "document_count": len(documents),
                        "top_score": max(scores) if scores else None,
                        "documents": _doc_summary(list(documents)),
                    }
                )
            except Exception as error:
                retrieve_span.update(level="ERROR", status_message=str(error))
                raise
            finally:
                retrieve_span.end()

        if request.stream:
            yield self._thought(
                session_id=session_id,
                model=request.model,
                stage=PROGRESS_RETRIEVING,
                content=(
                    "回顾会话历史，结合上文回答…"
                    if plan.query_type == "recall"
                    else (
                        f"检索到 {len(documents)} 篇相关文档"
                        if documents
                        else "未检索到相关文档"
                    )
                ),
            )
            for idx, doc in enumerate(documents[:5], start=1):
                meta = getattr(doc, "metadata", None) or {}
                source = meta.get("source") or meta.get("filename") or "未知来源"
                snippet = (getattr(doc, "page_content", "") or "").strip().replace("\n", " ")
                if len(snippet) > 60:
                    snippet = snippet[:60] + "…"
                yield self._thought(
                    session_id=session_id,
                    model=request.model,
                    stage=PROGRESS_RETRIEVING,
                    content=f"[{idx}] {source}" + (f" · {snippet}" if snippet else ""),
                )

        if request.return_direct:
            await _cancel_task(recall_task)
            root.update(
                output={
                    "finish_reason": "return_direct",
                    "document_count": len(documents),
                }
            )
            yield KnowledgeQaEvent(
                kind="complete",
                session_id=session_id,
                documents=tuple(documents),
                finish_reason="stop",
            )
            return

        # 上下文回忆但会话无历史：直接固定回复，不调 LLM，避免模型凭空编造"上一轮内容"
        if plan.query_type == "recall" and not history:
            empty_answer = "当前会话还没有可回顾的历史记录，请先提问后再问我刚才聊了什么。"
            if request.stream:
                yield KnowledgeQaEvent(
                    kind="start",
                    session_id=session_id,
                    model=request.model,
                )
                yield KnowledgeQaEvent(
                    kind="token",
                    session_id=session_id,
                    model=request.model,
                    content=empty_answer,
                )
                self._memory.save_turn(session, request.query, empty_answer)
                yield KnowledgeQaEvent(
                    kind="complete",
                    session_id=session_id,
                    model=request.model,
                    content="",
                    finish_reason="stop",
                )
                _spawn_background(
                    self._post_stream_finalize(request, session, session_id, empty_answer)
                )
                await _cancel_task(recall_task)
                return
            self._memory.save_turn(session, request.query, empty_answer)
            self._memory.maybe_summarize(
                session,
                model=request.model or self._chat_model.default_model(),
            )
            await self._write_long_term_memory(
                request, session_id, request.query, empty_answer
            )
            await _cancel_task(recall_task)
            yield KnowledgeQaEvent(
                kind="complete",
                session_id=session_id,
                model=request.model,
                content=empty_answer,
                finish_reason="stop",
            )
            return

        # 拒答判定（两层来源）：
        # 1) 真正检索过但无证据 -> 可能属知识缺口，登记工单；
        # 2) 未检索但非寒暄（planner 把与知识库无关的实质性问题误放行）
        #    -> 视为无依据直接拒答，不登记工单，避免对无关问题自由发挥。
        should_refuse = bool(plan.need_retrieval and not documents)
        if not should_refuse and (not plan.need_retrieval and plan.query_type not in ("chitchat", "recall")):
            should_refuse = True
        # 仅对疑似真实知识缺口登记工单；query_type=other（与知识库无关的实质问题，
        # 如游戏/私事）、chitchat（寒暄）、recall（上下文回忆）均不登记，避免污染工单库。
        create_ticket = bool(
            should_refuse
            and plan.need_retrieval
            and plan.query_type not in ("other", "chitchat", "recall")
        )

        if should_refuse:
            ticket_id = None
            refuse_span = self._trace.start_span(
                "refusal",
                as_type="span",
                input={
                    "need_retrieval": plan.need_retrieval,
                    "query_type": plan.query_type,
                    "create_ticket": create_ticket,
                },
                parent=root,
            )
            with self._trace.activate(refuse_span):
                try:
                    if create_ticket and self._knowledge_gap is not None:
                        try:
                            ticket_id = await asyncio.to_thread(
                                self._knowledge_gap.create,
                                request.query,
                                request.knowledge_base_name,
                            )
                        except Exception as error:
                            logger.error(
                                "创建知识缺口工单失败(硬拒答) q=%r: %s",
                                request.query[:50],
                                error,
                            )
                    refuse_span.update(
                        output={"ticket_id": ticket_id, "answer": REFUSAL_NO_EVIDENCE}
                    )
                    root.update(
                        output={
                            "is_refusal": True,
                            "ticket_id": ticket_id,
                            "answer": REFUSAL_NO_EVIDENCE if capture else {"chars": len(REFUSAL_NO_EVIDENCE)},
                        }
                    )
                finally:
                    refuse_span.end()
            if request.stream:
                yield KnowledgeQaEvent(
                    kind="start",
                    session_id=session_id,
                    model=request.model,
                    ticket_id=ticket_id,
                    is_refusal=True,
                )
                yield KnowledgeQaEvent(
                    kind="token",
                    session_id=session_id,
                    model=request.model,
                    content=REFUSAL_NO_EVIDENCE,
                    is_refusal=True,
                )
                self._memory.save_turn(session, request.query, REFUSAL_NO_EVIDENCE)
                yield KnowledgeQaEvent(
                    kind="complete",
                    session_id=session_id,
                    model=request.model,
                    content="",
                    ticket_id=ticket_id,
                    finish_reason="stop",
                    is_refusal=True,
                )
                _spawn_background(
                    self._post_stream_finalize(
                        request, session, session_id, REFUSAL_NO_EVIDENCE
                    )
                )
                await _cancel_task(recall_task)
                return
            yield KnowledgeQaEvent(
                kind="complete",
                session_id=session_id,
                model=request.model,
                content=REFUSAL_NO_EVIDENCE,
                ticket_id=ticket_id,
                finish_reason="stop",
                is_refusal=True,
            )
            self._memory.save_turn(session, request.query, REFUSAL_NO_EVIDENCE)
            self._memory.maybe_summarize(
                session,
                model=request.model or self._chat_model.default_model(),
            )
            # 拒答也沉淀长期记忆：未解决问题本身就是高价值记忆（用户可能反复追问）
            await self._write_long_term_memory(
                request, session_id, request.query, REFUSAL_NO_EVIDENCE
            )
            await _cancel_task(recall_task)
            return

        if plan.query_type == "recall":
            # 上下文回忆：用会话历史回答，不接地检索文档
            prompt_template = RECALL_USER
            context = ""
            max_citations = 0
        elif plan.need_retrieval:
            # 按模型平台选择接地 prompt：lenient 平台（如 DeepSeek）放宽"直接回答"判定，
            # 避免把表述差异（"客户端"vs"移动端客户端"）误判为不足以回答而过度拒答。
            # 注：曾尝试弱证据分流（top_rel∈[0.35,0.65) 走 GROUNDED_WEAK_USER），
            # 实测 negative 题 rel 落入该区间被误伤（拒答→部分回答+幻觉），且受益题
            # 不走分流也答对，净负收益，已回滚（2026-08-31）。
            prompt_template = grounded_user(
                self._chat_model.grounding_style(request.model)
            )
            context = assemble_context(documents)
            max_citations = len(documents)
        else:
            # 仅当 planner 明确判定为 chitchat 才走闲聊分支；
            # 其余未检索情形已在上方 should_refuse 中拦截拒答。
            prompt_template = CHITCHAT_USER
            context = ""
            max_citations = 0
        try:
            # 长期记忆：生成前注入该用户跨会话已知信息（已与检索并行执行，失败不影响主链路）
            memory_context = await recall_task if recall_task is not None else ""
            messages: list[ChatMessage] = [
                dict(message) for message in self._memory.context_messages(session)
            ]
            if memory_context:
                messages.insert(0, {"role": "system", "content": memory_context})
            messages.append({
                "role": "user",
                "content": prompt_template.format(context=context, question=request.query),
            })
            gen_span = self._trace.start_span(
                "generating",
                as_type="span",
                input={
                    "message_count": len(messages),
                    "document_count": len(documents),
                    "has_memory_context": bool(memory_context),
                },
                parent=root,
                model=request.model,
            )
            with self._trace.activate(gen_span):
                try:
                    if request.stream:
                        yield KnowledgeQaEvent(
                            kind="start",
                            session_id=session_id,
                            model=request.model,
                            documents=tuple(documents),
                        )
                        yield self._progress(
                            session_id=session_id,
                            model=request.model,
                            stage=PROGRESS_GENERATING,
                        )
                        yield self._thought(
                            session_id=session_id,
                            model=request.model,
                            stage=PROGRESS_GENERATING,
                            content=(
                                "结合会话上文组织回答…"
                                if plan.query_type == "recall"
                                else (
                                    f"基于 {len(documents)} 篇参考文档组织答案…"
                                    if plan.need_retrieval
                                    else "组织回答…"
                                )
                            ),
                        )
                        # 越过 </think> 后增量放出；无闭标签时 finalize 再整段放出，避免裸思考泄漏。
                        think_filter = ThinkStreamFilter()
                        answer_parts: list[str] = []
                        async for token in self._chat_model.stream(
                            messages,
                            model=request.model,
                            temperature=request.temperature,
                            max_tokens=request.max_tokens,
                        ):
                            visible = think_filter.feed(token)
                            if not visible:
                                continue
                            answer_parts.append(visible)
                            yield KnowledgeQaEvent(
                                kind="token",
                                session_id=session_id,
                                model=request.model,
                                content=visible,
                            )
                        leftover = think_filter.finalize()
                        if leftover:
                            answer_parts.append(leftover)
                            yield KnowledgeQaEvent(
                                kind="token",
                                session_id=session_id,
                                model=request.model,
                                content=leftover,
                            )
                        answer = "".join(answer_parts)
                        if max_citations and answer:
                            answer, invalid = sanitize_citations(answer, max_index=max_citations)
                            if invalid:
                                logger.warning("流式回答含非法引用编号 %s", sorted(set(invalid)))
                    else:
                        answer = strip_think(
                            await self._chat_model.complete_async(
                                messages,
                                model=request.model,
                                temperature=request.temperature,
                                max_tokens=request.max_tokens,
                            )
                        )
                        if max_citations:
                            answer, invalid = sanitize_citations(answer, max_index=max_citations)
                            if invalid:
                                logger.warning("回答已剔除非法引用编号 %s", sorted(set(invalid)))
                    gen_span.update(
                        output=_trace_input_text(answer or "", capture=capture, limit=4000)
                    )
                    # 软拒答建单：检索到文档但 LLM 依据 prompt 规则判"无法回答"时，
                    # 同样登记知识缺口工单（此前只有"零文档硬拒答"建单，闭环断在中间）。
                    # 不置 is_refusal：保留 docs 透出，让用户看到检索到了什么。
                    soft_ticket_id = None
                    if (
                        plan.need_retrieval
                        and plan.query_type not in ("other", "chitchat")
                        and is_model_refusal(answer)
                        and self._knowledge_gap is not None
                    ):
                        try:
                            soft_ticket_id = await asyncio.to_thread(
                                self._knowledge_gap.create,
                                request.query,
                                request.knowledge_base_name,
                            )
                            logger.info(
                                "生成侧拒答已登记知识缺口工单 #%s q=%r",
                                soft_ticket_id,
                                request.query[:50],
                            )
                        except Exception as error:
                            logger.error(
                                "创建知识缺口工单失败(生成侧拒答) q=%r: %s",
                                request.query[:50],
                                error,
                            )
                    root.update(
                        output={
                            "is_refusal": False,
                            "soft_refusal": soft_ticket_id is not None,
                            "ticket_id": soft_ticket_id,
                            "document_count": len(documents),
                            "answer": _trace_input_text(answer or "", capture=capture, limit=4000),
                        }
                    )
                except Exception as error:
                    gen_span.update(level="ERROR", status_message=str(error))
                    raise
                finally:
                    gen_span.end()

            # 会话落库要尽快完成（下一问可能立刻跟上）；摘要 / 长期记忆很慢，流式时后台跑
            self._memory.save_turn(session, request.query, answer)
            if request.stream:
                yield KnowledgeQaEvent(
                    kind="complete",
                    session_id=session_id,
                    model=request.model,
                    content="",
                    documents=tuple(documents),
                    ticket_id=soft_ticket_id,
                    finish_reason="stop",
                )
                _spawn_background(
                    self._post_stream_finalize(request, session, session_id, answer)
                )
            else:
                self._memory.maybe_summarize(
                    session,
                    model=request.model or self._chat_model.default_model(),
                )
                await self._write_long_term_memory(
                    request, session_id, request.query, answer
                )
                yield KnowledgeQaEvent(
                    kind="complete",
                    session_id=session_id,
                    model=request.model,
                    content=answer,
                    documents=tuple(documents),
                    ticket_id=soft_ticket_id,
                    finish_reason="stop",
                )
        finally:
            await _cancel_task(recall_task)
