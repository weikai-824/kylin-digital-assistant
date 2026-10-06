"""对话页：左栏精简配置，中栏问答，右栏依据。"""

from __future__ import annotations

import html
import re
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import streamlit as st
import streamlit_antd_components as sac
from streamlit_chatbox import *
from streamlit_extras.bottom_container import bottom

from kylin.adapters.inbound.webui import stream_runtime
from kylin.adapters.inbound.webui.utils import ApiRequest
from kylin.adapters.outbound.model.clients import get_config_models, get_config_platforms, get_default_llm
from kylin.platform.runtime.net import api_address
from kylin.platform.config.settings import Settings

APP_NAME = "系统咨询数字助手"

# 流式刷新期间只渲染最近若干条消息（完整渲染留到流结束后那一帧）
STREAM_RENDER_LIMIT = 12
# 同一会话内重复提问的本地缓存条数
ANSWER_CACHE_MAX = 20

# 欢迎页示例问题：点击即发送
SAMPLE_QUESTIONS = [
    "年假怎么申请？",
    "事假和病假有什么区别？",
    "报销需要哪些材料？",
]

# 简易 SVG 头像，不依赖已删除的 png
_ASSISTANT_AVATAR = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='14' fill='%232f7f8a'/>"
    "<text x='32' y='42' text-anchor='middle' font-size='28' "
    "font-family='Georgia,serif' fill='%23f3fafb'>咨</text>"
    "</svg>"
)


def save_session(conv_name: str = None):
    chat_box.context_from_session(
        conv_name, exclude=["selected_page", "prompt", "cur_conv_name", "upload_image"]
    )


def restore_session(conv_name: str = None):
    chat_box.context_to_session(
        conv_name, exclude=["selected_page", "prompt", "cur_conv_name", "upload_image"]
    )


def restore_history_from_server(api: ApiRequest) -> None:
    """刷新页面后：URL 里带 session_id 则从服务端拉历史回填 chat_box。

    streamlit 的 st.session_state 刷新即清空，但 URL query param 保留；
    据此拿到 session_id → 后端只读接口取归档 → 重建 history 供 output_messages 渲染。
    """
    sid = st.query_params.get("session_id")
    if not sid:
        return
    try:
        data = api.get_session_history(sid)
    except Exception:
        return
    if not data or data.get("code") != 200:
        return
    # 回填成功即记住 session_id：否则刷新后 ctx 为空、URL 仍带 session_id，
    # kb_chat 会在**每一帧**重复进来拉历史并 reset_history()——刚提交的新提问
    # 会被抹掉，流式的答案还会经 update_msg 写进上一条旧消息里。
    chat_box.context["session_id"] = sid
    msgs = data.get("messages") or []
    if msgs:
        chat_box.reset_history()
        for m in msgs:
            chat_box.history.append(
                {
                    "role": m.get("role", "user"),
                    "elements": [Markdown(m.get("content", ""))],
                    "metadata": {},
                }
            )
    # 恢复会话所属知识库（仍存在则自动切回，避免记忆串库）
    kb_name = data.get("kb_name") or ""
    if kb_name:
        try:
            kb_raw = api.list_knowledge_bases() or []
            kb_list = [
                x["kb_name"]
                for x in kb_raw
                if isinstance(x, dict) and x.get("kb_name")
            ]
        except Exception:
            kb_list = []
        if kb_name in kb_list:
            st.session_state.selected_kb = kb_name


def rerun():
    save_session()
    st.rerun()


def get_messages_history(
    history_len: int, content_in_expander: bool = False
) -> List[Dict]:
    def filter(msg):
        content = [
            x for x in msg["elements"] if x._output_method in ["markdown", "text"]
        ]
        if not content_in_expander:
            content = [x for x in content if not x._in_expander]
        content = [x.content for x in content]
        return {"role": msg["role"], "content": "\n\n".join(content)}

    messages = chat_box.filter_history(history_len=history_len, filter=filter)
    if sys_msg := chat_box.context.get("system_message"):
        messages = [{"role": "system", "content": sys_msg}] + messages
    return messages


def add_conv(name: str = ""):
    conv_names = chat_box.get_chat_names()
    if not name:
        i = len(conv_names) + 1
        while True:
            name = f"会话{i}"
            if name not in conv_names:
                break
            i += 1
    if name in conv_names:
        sac.alert(
            "创建新会话出错",
            f"该会话名称 “{name}” 已存在",
            color="error",
            closable=True,
        )
    else:
        chat_box.use_chat_name(name)
        st.session_state["cur_conv_name"] = name


chat_box = ChatBox(assistant_avatar=_ASSISTANT_AVATAR)


def init_widgets():
    st.session_state.setdefault(
        "history_len", Settings.model_settings.HISTORY_LEN
    )
    st.session_state.setdefault(
        "selected_kb", Settings.kb_settings.DEFAULT_KNOWLEDGE_BASE
    )
    st.session_state.setdefault(
        "kb_top_k", Settings.kb_settings.VECTOR_SEARCH_TOP_K
    )
    st.session_state.setdefault(
        "score_threshold", Settings.kb_settings.SCORE_THRESHOLD
    )
    st.session_state.setdefault("return_direct", False)
    st.session_state.setdefault("cur_conv_name", chat_box.cur_chat_name)
    st.session_state.setdefault("last_conv_name", chat_box.cur_chat_name)
    st.session_state.setdefault("last_evidence_docs", [])
    st.session_state.setdefault("last_evidence_kb", "")
    st.session_state.setdefault("last_raw_docs", [])
    st.session_state.setdefault("last_ticket_id", None)
    st.session_state.setdefault("answer_feedback", [])
    st.session_state.setdefault("stream_run_id", "")
    st.session_state.setdefault("stream_question", "")
    st.session_state.setdefault("stream_kb", "")
    st.session_state.setdefault("qa_inflight", False)
    st.session_state.setdefault("qa_answer_cache", {})


def parse_evidence_docs(docs: Optional[List]) -> List[Tuple[str, str, str, str]]:
    """解析 format_reference 产出的字符串 → (idx, filename, url, body)。"""
    if not docs:
        return []
    out: List[Tuple[str, str, str, str]] = []
    for raw in docs:
        if not isinstance(raw, str):
            continue
        if "未找到相关文档" in raw or "已拒绝" in raw:
            continue
        m = re.search(
            r"出处\s*\[(\d+)\]\s*\[([^\]]+)\]\(([^)]*)\)\s*\n\n([\s\S]*)",
            raw.strip(),
        )
        if m:
            body = m.group(4).strip()
            if len(body) > 280:
                body = body[:280].rstrip() + "…"
            out.append((m.group(1), m.group(2), m.group(3), body))
        else:
            # 兜底：整段展示
            text = re.sub(r"<[^>]+>", "", raw).strip()
            if text:
                out.append(("?", "参考", "", text[:280]))
    return out


def _cancel_stream() -> None:
    """取消进行中的流式问答（切换会话 / 清空对话时调用）。"""
    run_id = st.session_state.get("stream_run_id") or ""
    if run_id and not str(run_id).startswith("pending"):
        stream_runtime.drop(run_id)
    st.session_state["stream_run_id"] = ""
    st.session_state["qa_inflight"] = False


def _history_nonempty() -> bool:
    """真实历史是否非空（欢迎 chips 与 streamlit_chatbox.history 属性要对齐）。"""
    try:
        if chat_box.history:
            return True
    except Exception:
        pass
    try:
        return bool(chat_box.filter_history(history_len=1))
    except Exception:
        return False


def _append_history(role: str, elements) -> None:
    """只写入 history，不在当帧再挂 st.chat_message。

    ``user_say`` / ``ai_say`` 会立刻渲染气泡；若本帧已经 ``output_messages``
    或渲染过欢迎 chips，新挂的 chat 容器会把后续控件吸进去，出现
    「气泡里套示例问题」和视觉双份。提交帧只改数据，下一帧统一输出。
    """
    chat_box.init_session()
    prepared = chat_box._prepare_elements(elements)
    chat_box.history.append(
        {"role": role, "elements": prepared, "metadata": {}}
    )


def _msg_text(msg) -> str:
    """取一条历史消息的纯文本（拼接所有字符串型元素）。"""
    parts = []
    for el in (msg or {}).get("elements") or []:
        content = getattr(el, "content", None)
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def _is_replay(prompt: str) -> bool:
    """本条提问是否只是「上一轮那一问一答」的重放。

    st.chat_input 属于 Streamlit 明令禁止用 ``st.session_state`` 赋值的 widget
    （赋值即抛 StreamlitAPIException），所以清空输入框这条常规路子走不通；
    改在提交侧识别重放：上一条 user 消息就是这段文本、且上一条 assistant 消息
    已不再是阶段占位（说明那一轮已经出过结果）→ 判定为重复提交。
    上一轮若仍停在阶段占位（那一轮没出结果），则视为补问，不做拦截。
    """
    hist = chat_box.history or []
    if len(hist) < 2:
        return False
    last, user = hist[-1], hist[-2]
    if last["role"] != "assistant" or user["role"] != "user":
        return False
    if _msg_text(user).strip() != (prompt or "").strip():
        return False
    return "qa-stages" not in _msg_text(last)


def _stage_html(stage: str, elapsed: float = 0.0) -> str:
    """阶段指示器：已完成打勾、进行中转圈、未开始灰显。

    后端早就 yield 了 planning/retrieving/generating 三个进度事件，但原先
    脚本同步阻塞，前端根本看不到——这里把它们渲染成会走动的进度条。
    """
    order = stream_runtime.STAGE_ORDER
    labels = stream_runtime.STAGE_LABELS
    idx = order.index(stage) if stage in order else 0
    items = []
    for i, key in enumerate(order):
        if i < idx:
            cls, mark = "done", "✓ "
        elif i == idx:
            cls, mark = "active", ""
        else:
            cls, mark = "todo", ""
        items.append(f'<span class="qa-stage {cls}">{mark}{labels[key]}</span>')
    tail = (
        f'<span class="qa-stage-tip">已等待 {elapsed:.0f}s</span>'
        if elapsed >= 3
        else ""
    )
    return f'<div class="qa-stages">{"".join(items)}{tail}</div>'


def render_messages(chat_box, *, limit: Optional[int] = None) -> None:
    """渲染历史消息；流式刷新期间只渲染最近若干条。

    ``output_messages()`` 每帧都会重建全部 chat_message 容器并重新渲染每条
    消息里的 markdown，会话一长，150ms 一帧就会明显发涩。流式期间临时把
    history 换成尾部切片，渲染完立刻换回，不触碰真实数据。
    """
    if not limit:
        chat_box.output_messages()
        return
    # history 是 property（只读），真实数据存在 session_state 的 dict 里
    store = st.session_state.get(chat_box._session_key, {}).get(
        chat_box.cur_chat_name, {}
    )
    full = store.get("history") or []
    if len(full) <= limit:
        chat_box.output_messages()
        return
    store["history"] = full[-limit:]
    try:
        chat_box.output_messages()
    finally:
        store["history"] = full


def parse_raw_docs(raw: Optional[List]) -> List[Tuple[str, str, str, str]]:
    """解析后端透出的结构化检索结果 → (idx, filename, score, body)。"""
    if not raw:
        return []
    out: List[Tuple[str, str, str, str]] = []
    for i, d in enumerate(raw, 1):
        if not isinstance(d, dict):
            continue
        meta = d.get("metadata") or {}
        filename = str(meta.get("source") or meta.get("title_path") or "参考")
        score = meta.get("score")
        score_txt = f"{float(score):.2f}" if score is not None else ""
        body = (d.get("page_content") or "").strip()
        out.append((str(i), filename, score_txt, body))
    return out


def _render_evidence_panel():
    """右侧依据面板（fragment：回答完成后局部刷新，不重跑全页）。"""
    docs = st.session_state.get("last_evidence_docs") or []
    raw = st.session_state.get("last_raw_docs") or []
    kb = st.session_state.get("last_evidence_kb") or ""
    ticket_id = st.session_state.get("last_ticket_id")

    st.markdown(
        f"""
        <div class="evidence-panel">
          <h3>依据</h3>
          <div class="sub">与回答中的 [1][2]… 对应{(' · ' + html.escape(kb)) if kb else ''}</div>
        """,
        unsafe_allow_html=True,
    )

    # 拒答 → 工单提示卡
    if ticket_id:
        st.markdown(
            f"""
            <div class="ticket-banner">
              <div class="tb-title">⚠️ 未找到相关文档，已拒绝无依据作答</div>
              <div class="tb-body">
                已自动登记为 <b>知识缺口工单 #{html.escape(str(ticket_id))}</b>。
                可到左侧「工单」页补充答案，反哺进知识库后，同类问题即可正常回答。
              </div>
            </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    items = parse_raw_docs(raw) or parse_evidence_docs(docs)

    if not items:
        st.markdown(
            '<div class="evidence-empty">提问后，检索到的制度原文会显示在这里。</div></div>',
            unsafe_allow_html=True,
        )
        return

    cards = []
    for idx, filename, score, body in items:
        # 文件名同样来自文档元数据，可被构造成 <img onerror=...> 之类的载荷
        src = html.escape(filename)
        score_badge = (
            f'<span class="score">相关度 {score}</span>' if score else ""
        )
        # body 是 KB 检索命中的文档原文，属不可信输入：必须转义后再拼 HTML，
        # 否则一份内嵌 <script> 或 <img onerror=...> 的文档即可在问答页执行任意 JS。
        safe_body = html.escape(body, quote=True)
        body_html = (
            f'<details class="ev-detail"><summary>展开原文（{len(body)} 字）</summary>'
            f'<div class="ev-body">{safe_body}</div></details>'
            if len(body) > 220
            else f'<div class="ev-body">{safe_body}</div>'
        )
        cards.append(
            f'<div class="evidence-card">'
            f'<div class="idx">[{idx}]</div>'
            f'<div class="src">{src}{score_badge}</div>'
            f'{body_html}'
            f"</div>"
        )
    st.markdown("".join(cards) + "</div>", unsafe_allow_html=True)

    _render_feedback()


def _render_feedback():
    """答案质量反馈：本地收集（待质量分析方案落地后统一入仓）。

    key 用静态值：evidence fragment 通过 st.rerun(scope="fragment") 局部重跑，
    不在同一运行帧内重复创建组件，静态 key 可保持按钮状态。
    """
    records = st.session_state.get("answer_feedback") or []
    st.markdown('<div class="evidence-feedback">', unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    # 不带显式 key：widget 状态由 Streamlit 按位置自动管理。
    # 显式 key（fb_up/fb_down）在 experimental_fragment 局部重跑时会触发
    # "cannot be set using st.session_state"，去掉 key 由框架位置标识规避。
    if c1.button("👍 有帮助", use_container_width=True):
        st.session_state.answer_feedback = records + [
            {"score": 1, "time": datetime.now().isoformat(timespec="seconds")}
        ]
        st.toast("已记录：有帮助")
    if c2.button("👎 没帮助", use_container_width=True):
        st.session_state.answer_feedback = records + [
            {"score": 0, "time": datetime.now().isoformat(timespec="seconds")}
        ]
        st.toast("已记录：没帮助（建议提交工单补充知识）")
    st.markdown("</div>", unsafe_allow_html=True)


# fragment 兼容层：1.37+ 用 st.fragment，1.33-1.36 用 st.experimental_fragment，更旧则退化（不局部刷新）
_frag_api = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)
_evidence_fragment = _frag_api(_render_evidence_panel) if _frag_api else None


@st.cache_data(ttl=20, show_spinner=False)
def _list_knowledge_bases(_api: ApiRequest):
    """知识库列表缓存。

    流式刷新期间脚本每 150ms 重跑一次，若每帧都打一次列表接口，
    等于把后端的 QPS 翻好几倍——这里按 20s 缓存。
    """
    return _api.list_knowledge_bases() or []


def _answer_cache() -> Dict:
    return st.session_state.setdefault("qa_answer_cache", {})


def _lookup_answer_cache(kb_name: str, prompt: str):
    """同一会话内重复提问直接复用上一条结果，省掉一整轮链路。"""
    return _answer_cache().get((kb_name, (prompt or "").strip()))


def _remember_answer(kb_name: str, prompt: str, snap) -> None:
    """把本轮结果写进会话内缓存（仅成功且有正文时）。"""
    if not kb_name or not prompt or snap.error or not snap.text:
        return
    cache = _answer_cache()
    if len(cache) >= ANSWER_CACHE_MAX:
        cache.pop(next(iter(cache)))
    cache[(kb_name, prompt.strip())] = (
        snap.text,
        list(snap.docs),
        list(snap.raw_docs),
        snap.ticket_id,
    )


def _advance_stream(snap) -> None:
    """渲染当前快照：有答案就逐字流出，没答案就推进阶段指示器。"""
    if snap.error:
        _finish_stream(snap)
        return
    if snap.text:
        chat_box.update_msg(snap.text.replace("\n", "\n\n"), streaming=True)
    else:
        chat_box.update_msg(
            _stage_html(snap.stage or stream_runtime.STAGE_ORDER[0], snap.elapsed),
            streaming=False,
        )
    time.sleep(stream_runtime.REFRESH_INTERVAL)
    st.rerun()


def _finish_stream(snap) -> None:
    """流结束：落定正文、记录 session_id 与结果缓存、清理运行时。"""
    if snap.error:
        chat_box.update_msg(
            f'<span class="qa-error">⚠️ {html.escape(snap.error)}</span>',
            streaming=False,
        )
    elif snap.text:
        chat_box.update_msg(snap.text, streaming=False)
    elif snap.interrupted:
        chat_box.update_msg("（已停止生成）", streaming=False)
    else:
        chat_box.update_msg("", streaming=False)

    ctx = chat_box.context
    if snap.session_id:
        ctx["session_id"] = snap.session_id
        st.query_params["session_id"] = snap.session_id

    _remember_answer(
        st.session_state.get("stream_kb", ""),
        st.session_state.get("stream_question", ""),
        snap,
    )
    stream_runtime.drop(snap.run_id)
    st.session_state["stream_run_id"] = ""
    st.session_state["qa_inflight"] = False
    st.rerun()


def _submit_question(
    api: ApiRequest,
    prompt: str,
    selected_kb: str,
    llm_model: str,
    *,
    history_len: int,
    kb_top_k: int,
    score_threshold: float,
    return_direct: bool,
):
    """提交一轮问答：先锁住提交 → 只写 history → 后台拉流 → rerun 再渲染。

    调用方必须已设置 ``stream_run_id=pending-…`` / ``qa_inflight``，防止双提交。
    """
    ctx = chat_box.context
    history = get_messages_history(ctx.get("history_len", history_len))
    # 新一轮提问清掉上一轮拒答的工单提示
    st.session_state["last_ticket_id"] = None
    st.session_state["last_evidence_docs"] = []
    st.session_state["last_raw_docs"] = []

    cached = _lookup_answer_cache(selected_kb, prompt)
    if cached is not None:
        answer, docs, raw_docs, ticket_id = cached
        if _is_replay(prompt):
            # 输入框重放：只把右侧依据补回并提示，不再写一遍一问一答，
            # 否则同一条提问会在页面上翻倍（后端也会多打一遍链路）。
            st.session_state["last_evidence_docs"] = list(docs)
            st.session_state["last_raw_docs"] = list(raw_docs)
            st.session_state["last_ticket_id"] = ticket_id
            st.session_state["last_evidence_kb"] = selected_kb
            st.session_state["stream_run_id"] = ""
            st.session_state["qa_inflight"] = False
            st.toast("已复用本次会话中的同一提问结果")
            st.rerun()
            return
        st.session_state["last_evidence_docs"] = list(docs)
        st.session_state["last_raw_docs"] = list(raw_docs)
        st.session_state["last_ticket_id"] = ticket_id
        st.session_state["last_evidence_kb"] = selected_kb
        _append_history("user", prompt)
        _append_history("assistant", answer)
        st.session_state["stream_run_id"] = ""
        st.session_state["qa_inflight"] = False
        st.toast("已复用本次会话中的同一提问结果")
        st.rerun()
        return

    _user = st.session_state.get("user") or {}
    extra_body = dict(
        top_k=kb_top_k,
        score_threshold=score_threshold,
        temperature=ctx.get("temperature"),
        prompt_name="default",
        return_direct=return_direct,
        session_id=ctx.get("session_id"),
        user_id=_user.get("userid") or None,
        dept=_user.get("dept") or None,
    )
    # WebUI 与 API 同机部署，走 loopback 免鉴权；
    # 切勿用 is_public=True（会走局域网 IP 触发 API token 校验 → 401）
    try:
        run_id = stream_runtime.start(
            api_url=api_address(),
            kb_name=selected_kb,
            model=llm_model,
            messages=history + [{"role": "user", "content": prompt}],
            extra_body=extra_body,
        )
    except Exception:
        st.session_state["stream_run_id"] = ""
        st.session_state["qa_inflight"] = False
        raise
    st.session_state["stream_run_id"] = run_id
    st.session_state["stream_question"] = prompt
    st.session_state["stream_kb"] = selected_kb
    # 单元素气泡：阶段/答案都写在正文；依据只走右侧面板，避免 status 转圈与双 expander
    _append_history("user", prompt)
    _append_history(
        "assistant",
        _stage_html(stream_runtime.STAGE_ORDER[0]),
    )
    st.rerun()


def kb_chat(api: ApiRequest):
    ctx = chat_box.context
    ctx.setdefault("uid", uuid.uuid4().hex)
    ctx.setdefault("session_id", None)  # 会话记忆：后端返回后携带，按会话隔离
    ctx.setdefault("llm_model", get_default_llm())
    ctx.setdefault("temperature", Settings.model_settings.TEMPERATURE)
    init_widgets()

    # 刷新恢复：st.session_state 已清空但 URL 里的 session_id 仍在 → 拉历史回填
    if not ctx.get("session_id"):
        restore_history_from_server(api)

    if st.session_state.cur_conv_name != st.session_state.last_conv_name:
        _cancel_stream()
        save_session(st.session_state.last_conv_name)
        restore_session(st.session_state.cur_conv_name)
        st.session_state.last_conv_name = st.session_state.cur_conv_name

    @st.experimental_dialog("模型配置", width="large")
    def llm_model_setting():
        cols = st.columns(3)
        platforms = ["所有"] + list(get_config_platforms())
        platform = cols[0].selectbox("选择模型平台", platforms, key="platform")
        llm_models = list(
            get_config_models(
                model_type="llm",
                platform_name=None if platform == "所有" else platform,
            )
        )
        llm_models += list(
            get_config_models(
                model_type="image2text",
                platform_name=None if platform == "所有" else platform,
            )
        )
        cols[1].selectbox("选择LLM模型", llm_models, key="llm_model")
        cols[2].slider("Temperature", 0.0, 1.0, key="temperature")
        st.text_area("System Message:", key="system_message")
        if st.button("确定"):
            rerun()

    # ---- 侧栏：知识库 ----
    with st.sidebar:
        st.caption("当前知识库")
        kb_raw = _list_knowledge_bases(api)
        kb_list = [x["kb_name"] for x in kb_raw if isinstance(x, dict) and x.get("kb_name")]
        if not kb_list:
            st.warning("暂无知识库或后端服务未就绪，请先到「知识库」页创建。")
            selected_kb = st.session_state.get("selected_kb") or ""
        else:
            if st.session_state.get("selected_kb") not in kb_list:
                st.session_state.selected_kb = kb_list[0]

            def on_kb_change():
                st.toast(f"已切换：{st.session_state.selected_kb}")

            selected_kb = st.selectbox(
                "知识库",
                kb_list,
                on_change=on_kb_change,
                key="selected_kb",
                label_visibility="collapsed",
            )

        if st.button("🆕 新对话", use_container_width=True):
            _cancel_stream()
            add_conv()
            rerun()

        now = datetime.now()
        st.download_button(
            "📄 导出对话记录",
            "".join(chat_box.export2md()),
            file_name=f"{now:%Y-%m-%d_%H.%M}_对话记录.md",
            mime="text/markdown",
            use_container_width=True,
        )

        history_len = st.session_state.history_len
        kb_top_k = st.session_state.kb_top_k
        score_threshold = st.session_state.score_threshold
        return_direct = st.session_state.return_direct

    # ---- 流式状态：先把快照同步进 session_state，依据面板当帧就能更新 ----
    run_id = st.session_state.get("stream_run_id") or ""
    pending_lock = str(run_id).startswith("pending")
    snap = (
        stream_runtime.peek(run_id)
        if run_id and not pending_lock
        else None
    )
    if run_id and not pending_lock and snap is None:
        # 线程已回收（服务重启等），清掉悬空的 run_id
        st.session_state["stream_run_id"] = ""
        st.session_state["qa_inflight"] = False
        run_id = ""
    streaming = snap is not None and not snap.done
    stream_busy = bool(run_id) or bool(st.session_state.get("qa_inflight"))
    if snap is not None and snap.docs_ready:
        st.session_state["last_evidence_docs"] = list(snap.docs)
        st.session_state["last_raw_docs"] = list(snap.raw_docs)
        st.session_state["last_ticket_id"] = snap.ticket_id
        st.session_state["last_evidence_kb"] = st.session_state.get("stream_kb", "")

    # ---- 主区：对话 | 依据 ----
    col_chat, col_evidence = st.columns([2.35, 1], gap="large")

    with col_chat:
        st.markdown(
            f"""
            <div class="main-header">
              <h1>{APP_NAME}</h1>
              <p class="hint">基于企业知识库的制度与流程咨询 · 回答将标注依据编号</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        has_history = _history_nonempty()

        # 消息必须先于欢迎区/示例问题渲染——否则 streamlit_chatbox 的
        # chat_message 会把当前活动容器吸进最后一个 element 的 empty()，
        # 导致 chips 与 expander 视觉重叠甚至嵌套。
        render_messages(chat_box, limit=STREAM_RENDER_LIMIT if streaming else None)

        # 流式/提交中绝不渲染 chips，避免被吸进助手气泡
        if not has_history and not stream_busy:
            st.markdown(
                """
                <div class="welcome">
                  <h3>有什么制度或流程想查？</h3>
                  <p>直接在下方输入框提问，回答会标注依据来源。</p>
                </div>
                """,
                unsafe_allow_html=True,
            )
            chip_cols = st.columns(len(SAMPLE_QUESTIONS))
            for i, (col, q) in enumerate(zip(chip_cols, SAMPLE_QUESTIONS)):
                if col.button(q, key=f"sample_q_{i}", use_container_width=True):
                    st.session_state["pending_query"] = q

    with col_evidence:
        # 流式期间不用 fragment，避免与主脚本 150ms rerun 交错造成状态错乱
        if streaming or _evidence_fragment is None:
            _render_evidence_panel()
        else:
            _evidence_fragment()

    llm_model = ctx.get("llm_model")
    chat_input_placeholder = "输入问题，Shift+Enter 换行"

    with bottom():
        cols = st.columns([1, 0.15, 14, 1])
        if streaming:
            if cols[0].button("⏹", help="停止生成"):
                stream_runtime.stop(run_id)
                st.toast("已请求停止，剩余内容将不再显示")
        elif cols[0].button("⚙️", help="模型配置"):
            widget_keys = ["platform", "llm_model", "temperature", "system_message"]
            chat_box.context_to_session(include=widget_keys)
            llm_model_setting()
        if not stream_busy and cols[-1].button("🗑️", help="清空对话"):
            _cancel_stream()
            chat_box.reset_history()
            ctx["session_id"] = None  # 清空对话即新会话
            if "session_id" in st.query_params:
                del st.query_params["session_id"]
            st.session_state["last_evidence_docs"] = []
            st.session_state["last_raw_docs"] = []
            st.session_state["last_ticket_id"] = None
            _answer_cache().clear()
            rerun()
        prompt = cols[2].chat_input(
            chat_input_placeholder,
            key="prompt",
            disabled=stream_busy,
        )

    # 任何 busy 状态都丢弃输入，杜绝二次提交（日志曾出现同问两次 chat/completions）
    if stream_busy:
        prompt = None
        st.session_state.pop("pending_query", None)
    elif not prompt:
        prompt = st.session_state.pop("pending_query", None)

    if snap is not None:
        if snap.done:
            _finish_stream(snap)
        else:
            _advance_stream(snap)
    elif prompt and selected_kb:
        # 原子锁必须先于 start / 写 history，挡住同会话连点与重复 script run
        st.session_state["qa_inflight"] = True
        st.session_state["stream_run_id"] = f"pending-{uuid.uuid4().hex}"
        _submit_question(
            api,
            prompt,
            selected_kb,
            llm_model,
            history_len=history_len,
            kb_top_k=kb_top_k,
            score_threshold=score_threshold,
            return_direct=return_direct,
        )
    elif prompt and not selected_kb:
        st.warning("请先选择或创建知识库。")
