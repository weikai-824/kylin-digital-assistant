"""会话记忆管理器：分层记忆（工作记忆原文 + 短期记忆滚动摘要）的组装 / 触发 / 保存。

设计依据：业界成熟方案（LangChain/LangGraph 三件套 + ChatGPT/Claude 分层实践）落地：
- 工作记忆：最近 RECENT_TURNS 轮原文完整保留，保证当前指代消解（query_plan standalone_query）不丢细节
- 短期记忆：更早轮次 → 增量滚动摘要（LLM 压缩），放生成 prompt 的 System 位置
- token 预算：历史总预算 HISTORY_TOKEN_BUDGET，超出从最早轮次砍，保底最近 MIN_KEEP_TURNS 轮
- 持久化：MySQL conversation_session 表（原文归档 messages_json + 摘要 summary + 累计轮数 turn_count）

跨会话长期记忆不做（OA 制度咨询场景，用户偏好类记忆价值有限，且有隐私风险）。
"""

import json
import re
from typing import List, Optional

from kylin.platform.runtime.messages import Message
from kylin.platform.config.settings import Settings

from kylin.adapters.outbound.mysql.database.models.conversation_session_model import ConversationSessionModel
from kylin.adapters.outbound.mysql.database.repository.conversation_session_repository import (
    get_or_create_session,
    get_session,
    update_session_fields,
)
from kylin.platform.runtime.schemas import History
from kylin.adapters.outbound.model.clients import get_ChatOpenAI, get_default_llm
from kylin.platform.observability.logging import build_logger

logger = build_logger()

# ---- 分层参数（如需调整直接改这里） ----
RECENT_TURNS = Settings.model_settings.HISTORY_LEN  # 工作记忆：与 query_plan 共用配置源
MIN_KEEP_TURNS = 2            # token 超预算时保底保留的最近轮数
HISTORY_TOKEN_BUDGET = 2500   # 历史总 token 预算（摘要 + 原文）
SUMMARY_TRIGGER_TURNS = 5     # 累计轮数每到 5 的倍数且超过窗口时触发一次摘要
SUMMARY_TRIGGER_TOKENS = 3000  # 或原文估算 token 超此值立即触发
SUMMARY_BUDGET_CHARS = 800    # 摘要长度上限（字符）
MAX_ARCHIVED_TURNS = 40       # messages_json 归档上限（摘要未及时触发时的保险）


def _estimate_tokens(text: str) -> int:
    """中英混合粗估：中文 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token。仅用于预算控制。"""
    if not text:
        return 0
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    return cn + (len(text) - cn) // 4 + 1


def _history_to_messages(items: List[dict]) -> List[Message]:
    """历史条目 -> OpenAI 消息 dict 列表。"""
    out: List[Message] = []
    for m in items:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role in ("ai", "assistant"):
            out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": "user", "content": content})
    return out


class ConversationManager:
    """会话记忆的门面：加载、组装、触发摘要、保存。"""

    # ---------- 加载 ----------

    def get_or_create(self, session_id: Optional[str], kb_name: str) -> ConversationSessionModel:
        return get_or_create_session(session_id=session_id, kb_name=kb_name)

    def get_by_id(self, session_id: str) -> Optional[ConversationSessionModel]:
        """只读加载会话（不新建、不更新活跃时间）。"""
        return get_session(session_id=session_id)

    def load_items(self, session: ConversationSessionModel) -> List[dict]:
        """公开的消息归档读取（供会话历史查询接口使用）。"""
        return self._load_items(session)

    def _load_items(self, session: ConversationSessionModel) -> List[dict]:
        raw = session.messages_json or ""
        try:
            items = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "session %s messages_json 解析失败，按空历史处理", session.session_id
            )
            items = []
        return items if isinstance(items, list) else []

    def load_history(self, session: ConversationSessionModel) -> List[History]:
        """从归档还原 History 列表，供 query_plan 指代消解使用。"""
        items = self._load_items(session)
        return [
            History(role=i.get("role", "user"), content=i.get("content", ""))
            for i in items
        ]

    # ---------- 生成侧组装 ----------

    def assemble_context_messages(
        self,
        session: ConversationSessionModel,
        *,
        max_tokens: int = HISTORY_TOKEN_BUDGET,
    ) -> List[Message]:
        """
        组装生成侧历史上下文（不含当前问题，由调用方追加模板消息）：
        [System(滚动摘要, 如有)] + 最近原文轮次。
        token 超预算时从最早原文轮次开始砍，保底最近 MIN_KEEP_TURNS 轮。
        """
        messages = _history_to_messages(self._load_items(session))
        summary = (session.summary or "").strip()
        if summary:
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "以下是你与用户之前对话的摘要（重要背景，回答相关问题时可引用，"
                        "与本次问题无关则忽略）：\n" + summary
                    ),
                },
            )
        if not messages:
            return messages

        estimated = sum(_estimate_tokens(m.get("content", "") or "") for m in messages)
        if estimated <= max_tokens:
            return messages

        # 超预算：system 摘要永不砍；原文从最早开始砍，保底最近 MIN_KEEP_TURNS 轮
        fixed: List[Message] = [messages[0]] if summary else []
        body = messages[1:] if summary else messages
        guarantee = body[-MIN_KEEP_TURNS * 2:] if len(body) > MIN_KEEP_TURNS * 2 else body
        prefix = body[: -MIN_KEEP_TURNS * 2] if len(body) > MIN_KEEP_TURNS * 2 else []

        keep_prefix = list(prefix)
        while keep_prefix:
            total = sum(
                _estimate_tokens(m.get("content", "") or "")
                for m in fixed + keep_prefix + guarantee
            )
            if total <= max_tokens:
                break
            keep_prefix = keep_prefix[2:]  # 丢最早一对（user+assistant），保持配对
        return fixed + keep_prefix + guarantee

    # ---------- 滚动摘要 ----------

    def _estimate_session_tokens(self, session: ConversationSessionModel) -> int:
        return sum(
            _estimate_tokens(str(i.get("content", "")))
            for i in self._load_items(session)
        )

    def needs_summary(self, session: ConversationSessionModel) -> bool:
        """触发条件：累计轮数超过工作窗口且（到 5 的倍数 或 原文 token 超阈值）。"""
        turns = session.turn_count or 0
        if turns <= RECENT_TURNS:
            return False
        over_turns = turns % SUMMARY_TRIGGER_TURNS == 0
        over_tokens = self._estimate_session_tokens(session) > SUMMARY_TRIGGER_TOKENS
        return over_turns or over_tokens

    def summarize(
        self,
        session: ConversationSessionModel,
        *,
        model: str = None,
        temperature: float = 0.2,
        max_tokens: int = 512,
    ) -> str:
        """
        增量滚动摘要：已有摘要 + 超窗口的新轮次 → LLM 合并出新摘要。
        失败不阻塞：返回旧摘要。
        """
        items = self._load_items(session)
        if len(items) <= RECENT_TURNS * 2:
            return session.summary or ""
        digest = items[: -(RECENT_TURNS * 2)]  # 超窗口的早期轮次
        old = (session.summary or "").strip()

        llm = get_ChatOpenAI(
            model_name=model or get_default_llm(),
            temperature=temperature,
            max_tokens=max_tokens,
            streaming=False,
            callbacks=[],
            verbose=False,
        )
        if llm is None:
            return old

        history_text = "\n".join(
            f"{'用户' if i.get('role') in ('user', 'human') else '助手'}: {i.get('content', '')}"
            for i in digest
        )
        if old:
            instruction = (
                f"这是已有对话摘要：\n{old}\n\n"
                f"以下是新产生的对话（较早部分），请把值得记住的关键事实、决策、"
                f"约束、用户偏好合并进摘要：\n{history_text}\n\n"
                f"输出更新后的完整摘要，{SUMMARY_BUDGET_CHARS} 字以内，只输出摘要本身。"
            )
        else:
            instruction = (
                f"请将以下对话压缩为摘要，保留关键事实、决策、用户偏好与约束，"
                f"{SUMMARY_BUDGET_CHARS} 字以内，只输出摘要本身：\n{history_text}"
            )
        try:
            resp = llm.invoke([{"role": "user", "content": instruction}])
            content = getattr(resp, "content", "") or ""
            if isinstance(content, list):
                content = "".join(str(getattr(b, "text", None) or b) for b in content)
            new_summary = str(content).strip()
            if new_summary:
                return new_summary[: SUMMARY_BUDGET_CHARS * 2]
        except Exception as e:
            logger.warning("滚动摘要失败，保留旧摘要: %s", e)
        return old

    def maybe_run_summary(self, session: ConversationSessionModel, *, model: str = None) -> bool:
        """检查并执行滚动摘要；执行后清理 messages_json 只留工作窗口。返回是否执行。"""
        if not self.needs_summary(session):
            return False
        new_summary = self.summarize(session, model=model)
        items = self._load_items(session)
        kept = items[-(RECENT_TURNS * 2):]
        kept_json = json.dumps(kept, ensure_ascii=False)
        update_session_fields(
            session.session_id,
            summary=new_summary,
            messages_json=kept_json,
        )
        # 同步 detached 实例内存属性，供后续 load_history / assemble 直接读取
        session.summary = new_summary
        session.messages_json = kept_json
        logger.info(
            "session %s 滚动摘要完成: %d→%d 条消息, 摘要 %d 字",
            session.session_id, len(items), len(kept), len(new_summary),
        )
        return True

    # ---------- 保存 ----------

    def save_turn(self, session: ConversationSessionModel, query: str, answer: str):
        """追加一轮问答到归档并更新累计轮数。"""
        items = self._load_items(session)
        q = (query or "").strip()
        a = (answer or "").strip()
        if not q and not a:
            return
        if q:
            items.append({"role": "user", "content": q})
        if a:
            items.append({"role": "assistant", "content": a})
        # 归档上限保险（摘要未及时触发时防止无限膨胀）
        if len(items) > MAX_ARCHIVED_TURNS * 2:
            items = items[-(MAX_ARCHIVED_TURNS * 2):]
        new_json = json.dumps(items, ensure_ascii=False)
        new_turns = (session.turn_count or 0) + 1
        update_session_fields(
            session.session_id,
            messages_json=new_json,
            turn_count=new_turns,
        )
        # 同步 detached 实例内存属性，供后续 load_history / assemble 直接读取
        session.messages_json = new_json
        session.turn_count = new_turns


# 模块级单例，供 rag_chat 直接使用
manager = ConversationManager()
