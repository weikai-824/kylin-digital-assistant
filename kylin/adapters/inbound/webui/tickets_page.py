"""知识缺口工单入站页面：查待办、人工回答并跟踪验证状态。"""

from __future__ import annotations

from datetime import datetime

import streamlit as st
import streamlit_antd_components as sac

STATUS_META = {
    "OPEN": ("待处理", "orange"),
    "RESOLVED": ("验证中", "blue"),
    "CLOSED": ("已闭环", "green"),
}


def _fetch_tickets(api, status: str = None) -> list:
    params = {"status": status} if status else None
    r = api.get("/tickets", params=params)
    if r is None:
        # 网络错误/后端不可达 ≠ 空列表：明确提示，避免被当作「没有工单」
        st.error("无法连接后端服务，请检查 API 服务是否启动")
        return []
    try:
        data = r.json()
    except Exception:
        return []
    if data.get("code") != 200:
        st.error(data.get("msg", "工单列表获取失败"))
        return []
    return data.get("data") or []


def _fmt(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def _render_ticket_card(api, t: dict, status_filter: str):
    tid = t["id"]
    status = t["status"]
    label, color = STATUS_META.get(status, (status, "gray"))
    review = t.get("needs_review")
    # streamlit-antd-components 0.3.1 没有 Badge / 单数 tag 组件：
    # 正确 API 是 sac.tags(items, color=, radius=, size=)，items 为 str 列表或 Tag
    badge_text = f"#{tid} · {label}{' · 需复核' if review else ''}"
    badge_color = color if not review else "red"

    with st.container(border=True):
        sac.tags(
            [badge_text],
            color=badge_color,
            radius="sm",
            size="sm",
        )
        st.markdown(f"**{t['question'][:120]}{'…' if len(t['question']) > 120 else ''}**")
        st.caption(
            f"创建 {_fmt(t.get('create_time'))} · 解决 {_fmt(t.get('resolve_time'))}"
            + (" · 已反哺入库" if t.get("fed_back") else " · 未入库")
        )

        if status == "OPEN":
            with st.form(f"ticket_answer_{tid}"):
                answer = st.text_area(
                    "人工答案（将反哺进独立知识库，请给出准确、可入库的内容）",
                    key=f"ta_{tid}",
                    height=110,
                )
                submitted = st.form_submit_button(
                    "提交答案并解决", type="primary", use_container_width=True
                )
            if submitted:
                if not (answer or "").strip():
                    st.warning("答案不能为空")
                else:
                    r = api.post(f"/tickets/{tid}/answer", json={"answer": answer})
                    if r is not None:
                        data = r.json()
                        if data.get("code") == 200:
                            st.toast(f"工单 #{tid} 已解决，答案反哺入库，验证中")
                            st.rerun()
                        else:
                            st.error(data.get("msg", "提交失败"))

        elif status == "RESOLVED":
            if review:
                st.warning(
                    "自动重试后仍未命中，已挂起人工复核：可修改答案重新提交，或确认后关闭。"
                )
                with st.form(f"ticket_reanswer_{tid}"):
                    answer = st.text_area(
                        "修改答案（重新反哺并验证）",
                        value=t.get("answer") or "",
                        key=f"tra_{tid}",
                        height=110,
                    )
                    resubmit = st.form_submit_button(
                        "修改答案并重新验证", type="primary", use_container_width=True
                    )
                if resubmit:
                    r = api.post(f"/tickets/{tid}/answer", json={"answer": answer})
                    if r is not None:
                        data = r.json()
                        if data.get("code") == 200:
                            st.toast(f"工单 #{tid} 已重新反哺")
                            st.rerun()
                        else:
                            st.error(data.get("msg", "提交失败"))
            else:
                st.info("答案已反哺入库，正在验证能否重新命中…（自动重试 5 分钟后仍失败会挂起复核）")
                if t.get("answer"):
                    with st.expander("查看已提交答案"):
                        st.write(t["answer"])

            c1, c2 = st.columns(2)
            if c1.button("重新验证", key=f"rv_{tid}", use_container_width=True):
                r = api.post(f"/tickets/{tid}/reverify", json={})
                if r is not None:
                    data = r.json()
                    if data.get("code") == 200:
                        st.toast("已重新验证")
                        st.rerun()
                    else:
                        st.error(data.get("msg", "验证失败"))
            if c2.button("关闭归档", key=f"cl_{tid}", use_container_width=True):
                r = api.post(f"/tickets/{tid}/close", json={})
                if r is not None:
                    data = r.json()
                    if data.get("code") == 200:
                        st.toast(f"工单 #{tid} 已关闭")
                        st.rerun()
                    else:
                        st.error(data.get("msg", "关闭失败"))

        else:  # CLOSED
            if t.get("answer"):
                with st.expander("查看人工答案"):
                    st.write(t["answer"])

        # 删除工单（所有状态可见）
        # 用 session_state 两阶段确认而非 st.popover：popover 在 streamlit 1.34 是
        # 位置化状态，删除后 rerun 会按位置重绑到下一张卡，导致孤儿弹层误删下一张工单。
        # 全部 key 带 tid 唯一化，状态与渲染位置解耦。
        st.divider()
        confirm_key = f"confirm_delete_{tid}"
        if st.session_state.get(confirm_key):
            st.warning("删除后不可恢复！若该工单已反哺知识库，会同步清理对应内容。")
            cc1, cc2 = st.columns(2)
            if cc1.button("取消", key=f"cancel_del_{tid}", use_container_width=True):
                st.session_state.pop(confirm_key, None)
                st.rerun()
            if cc2.button(
                "确认删除",
                key=f"del_confirm_{tid}",
                type="secondary",
                use_container_width=True,
            ):
                r = api.delete(f"/tickets/{tid}")
                if r is not None:
                    data = r.json()
                    if data.get("code") == 200:
                        st.session_state.pop(confirm_key, None)
                        st.toast(f"工单 #{tid} 已删除")
                        st.rerun()
                    else:
                        st.error(data.get("msg", "删除失败"))
        else:
            if st.button(
                f"🗑 删除工单 #{tid}",
                key=f"del_trigger_{tid}",
                use_container_width=True,
            ):
                st.session_state[confirm_key] = True
                st.rerun()


def tickets_page(api):
    st.markdown(
        """
        <div class="main-header">
          <h1>知识缺口工单</h1>
          <p class="hint">答不上来的问题会自动转成工单 · 人工补充答案后知识库越用越准</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    filters = {
        "全部": None,
        "待处理": "OPEN",
        "验证中": "RESOLVED",
        "需复核": "REVIEW",
        "已闭环": "CLOSED",
    }
    keys = list(filters.keys())
    idx = keys.index(st.session_state.get("ticket_filter", "全部"))
    selected = sac.segmented(
        keys,
        key="ticket_filter",
        color="#2f7f8a",
        align="center",
    )

    if selected == "需复核":
        tickets = [t for t in _fetch_tickets(api) if t.get("needs_review")]
    else:
        tickets = _fetch_tickets(api, status=filters[selected])

    if not tickets:
        st.info("没有符合条件的工单。" if selected == "全部" else f"没有「{selected}」状态的工单。")
        return

    st.caption(f"共 {len(tickets)} 条")
    for t in tickets:
        _render_ticket_card(api, t, filters[selected])
