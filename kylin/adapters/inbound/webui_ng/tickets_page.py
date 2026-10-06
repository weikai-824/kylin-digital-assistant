"""知识缺口工单处理页（NiceGUI）：取代旧 Streamlit 版 tickets_page.py。

8501 收敛后工单主入口（路由 /）。功能对齐：
1. 状态过滤（全部 / OPEN / RESOLVED / 需复核 / CLOSED）
2. OPEN：填答案并解决 → POST /tickets/{id}/answer
3. RESOLVED+review：改答案重新反哺
4. RESOLVED：手动重验 / 关闭归档
5. 任意状态：两段确认删除（dialog 二次确认）
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from nicegui import ui

from kylin.adapters.inbound.webui.utils import ApiRequest
from kylin.platform.runtime.net import api_address

logger = logging.getLogger(__name__)

APP_NAME = "知识缺口工单 · 调试台"

STATUS_META: Dict[str, tuple[str, str]] = {
    "OPEN": ("待处理", "orange"),
    "RESOLVED": ("验证中", "blue"),
    "CLOSED": ("已闭环", "green"),
}

# 状态过滤按钮与后端 status 参数的映射。
# "需复核" 走 needs_review=True 客户端筛选（/tickets 不直接支持该过滤参数）。
FILTERS: List[tuple[str, Optional[str]]] = [
    ("全部", None),
    ("待处理", "OPEN"),
    ("验证中", "RESOLVED"),
    ("需复核", None),  # 走客户端 needs_review 筛选
    ("已闭环", "CLOSED"),
]

PAGE_CSS = """
<style>
.ticket-card { border: 1px solid #e3e9f0; border-radius: 12px; padding: 14px 16px; background: #fff; }
.ticket-card .head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ticket-card .q { font-size: 15px; font-weight: 500; color: #15202b; line-height: 1.5;
  white-space: pre-wrap; word-break: break-word; }
.ticket-card .meta { font-size: 12px; color: #6d7f90; }
.filter-active { background: #2f7f8a !important; color: #fff !important; }
</style>
"""


def _fmt(ts: Optional[str]) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


async def _fetch_tickets(
    api: ApiRequest, status: Optional[str] = None
) -> List[Dict[str, Any]]:
    """拉工单列表；网络/后端异常 → 空列表 + 日志（不抛 UI 异常）。"""
    params = {"status": status} if status else None
    try:
        r = await asyncio.to_thread(api.get, "/tickets", params=params)
    except Exception as exc:  # noqa: BLE001
        logger.error("工单列表请求失败: %s", exc)
        return []
    if r is None:
        return []
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return []
    if data.get("code") != 200:
        logger.error("工单列表错误: %s", data.get("msg"))
        return []
    return data.get("data") or []


RefreshCb = Callable[[], Awaitable[None]]


def _render_filter_bar(
    current_label: str, on_select: Callable[[str], Awaitable[None]]
) -> Dict[str, Any]:
    """用 button_group 实现 segmented-style 过滤器, 兼容 NiceGUI 1.4.34。"""
    state: Dict[str, Any] = {"current": current_label}

    async def _wrap(label: str) -> None:
        state["current"] = label
        await on_select(label)
        # 刷新高亮: 重设每个 button 的颜色（不重建, 避免丢失其他状态）
        for btn in state["buttons"]:
            btn.props(f"color={'primary' if btn.text == label else 'grey-7'} unelevated")

    with ui.row().classes("w-full items-center justify-center gap-2") as row:
        state["buttons"] = []
        for label, _ in FILTERS:
            is_active = label == current_label
            btn = ui.button(
                label,
                on_click=lambda l=label: asyncio.create_task(_wrap(l)),
            ).props(
                f"color={'primary' if is_active else 'grey-7'} "
                f"{'unelevated' if is_active else 'flat'} no-caps dense"
            )
            state["buttons"].append(btn)
    return state


def register_pages() -> None:
    @ui.page("/")
    async def tickets_page() -> None:  # noqa: ANN202
        ui.add_head_html(PAGE_CSS)
        ui.colors(primary="#2f7f8a")

        api = ApiRequest(base_url=api_address())

        # ---- 页头 ----
        with ui.row().classes("w-full max-w-[1400px] mx-auto items-baseline gap-4 pt-4"):
            ui.label("知识缺口工单").classes("text-h5 font-medium")
            ui.link("知识库管理 →", "/kb").classes("text-subtitle2")
            ui.label("答不上来的问题自动建单 · 人工补充答案反哺知识库").classes("text-caption")

        # ---- 过滤栏 + 列表 ----
        list_box = ui.column().classes("w-full max-w-[1400px] mx-auto gap-3 pt-2 pb-8")

        async def _render_list(label: str) -> None:
            label_to_status = dict(FILTERS)
            status = label_to_status[label]
            tickets = await _fetch_tickets(api, status=status)
            if label == "需复核":
                tickets = [t for t in tickets if t.get("needs_review")]

            list_box.clear()
            with list_box:
                if not tickets:
                    msg = "没有符合条件的工单" if label == "全部" else f"没有「{label}」状态的工单"
                    with ui.row().classes("w-full justify-center pt-8"):
                        ui.label(msg).classes("text-grey-7")
                    return
                with ui.row().classes("w-full items-center"):
                    ui.label(f"共 {len(tickets)} 条").classes("text-caption text-grey-7")
                for t in tickets:
                    _render_card(api, t, lambda: _render_list(state["current"]))

        # 默认全部；_render_list 会再切到当前 label
        state = _render_filter_bar("全部", _render_list)
        # 初次填充
        await _render_list("全部")


def _render_card(
    api: ApiRequest, t: Dict[str, Any], refresh_cb: Callable[[], Awaitable[None]]
) -> None:
    tid = t["id"]
    status = t["status"]
    label, color = STATUS_META.get(status, (status, "grey"))
    review = t.get("needs_review")
    badge_text = f"#{tid} · {label}{' · 需复核' if review else ''}"
    badge_color = color if not review else "red"

    with ui.card().classes("w-full ticket-card gap-2"):
        # 头：状态徽标 + 时间 + 反哺标记
        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
            ui.badge(badge_text, color=badge_color).props("outline")
            ui.space()
            if t.get("fed_back"):
                ui.badge("已反哺入库", color="green-3").props("outline dense")
            ui.label(
                f"创建 {_fmt(t.get('create_time'))} · 解决 {_fmt(t.get('resolve_time'))}"
            ).classes("meta")

        # 问题正文
        q = t.get("question") or ""
        ui.label(q[:200] + ("…" if len(q) > 200 else "")).classes("q")

        # 按状态分支
        if status == "OPEN":
            _render_open_form(api, tid, refresh_cb)
        elif status == "RESOLVED":
            _render_resolved(api, tid, t, refresh_cb)
        else:  # CLOSED
            if t.get("answer"):
                with ui.expansion("查看已闭环答案", icon="description").classes("w-full"):
                    ui.label(t["answer"]).classes("text-caption whitespace-pre-wrap")

        # 删除（任意状态）
        _render_delete(api, tid, refresh_cb)


def _render_open_form(
    api: ApiRequest, tid: int, refresh_cb: Callable[[], Awaitable[None]]
) -> None:
    answer_in = (
        ui.textarea(
            "人工答案（将反哺进独立知识库，请给出准确、可入库的内容）",
        )
        .props("outlined dense rows=3")
        .classes("w-full")
    )

    async def _submit() -> None:
        ans = (answer_in.value or "").strip()
        if not ans:
            ui.notify("答案不能为空", type="warning")
            return
        try:
            ret = await asyncio.to_thread(
                api.post, f"/tickets/{tid}/answer", json={"answer": ans}
            )
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"请求失败：{exc}", type="negative")
            return
        if ret is None:
            ui.notify("提交失败：后端无响应", type="negative")
            return
        try:
            payload = ret.json()
        except Exception:  # noqa: BLE001
            ui.notify("提交失败：响应格式异常", type="negative")
            return
        if payload.get("code") == 200:
            ui.notify(f"工单 #{tid} 已解决，答案反哺入库，验证中", type="positive")
            await refresh_cb()
        else:
            ui.notify(payload.get("msg") or "提交失败", type="negative")

    ui.button("提交答案并解决", on_click=_submit).props("unelevated no-caps")


def _render_resolved(
    api: ApiRequest, tid: int, t: Dict[str, Any], refresh_cb: Callable[[], Awaitable[None]]
) -> None:
    if t.get("needs_review"):
        ui.label("自动重试后仍未命中，已挂起人工复核：可修改答案重新提交，或确认后关闭。").classes(
            "text-warning text-caption"
        )
        ans_in = (
            ui.textarea("修改答案（重新反哺并验证）", value=t.get("answer") or "")
            .props("outlined dense rows=3")
            .classes("w-full")
        )

        async def _resubmit() -> None:
            ans = (ans_in.value or "").strip()
            if not ans:
                ui.notify("答案不能为空", type="warning")
                return
            ret = await asyncio.to_thread(
                api.post, f"/tickets/{tid}/answer", json={"answer": ans}
            )
            if ret is not None and ret.json().get("code") == 200:
                ui.notify(f"工单 #{tid} 已重新反哺", type="positive")
                await refresh_cb()
            else:
                msg = ret.json().get("msg", "提交失败") if ret is not None else "提交失败"
                ui.notify(msg, type="negative")

        ui.button("修改答案并重新验证", on_click=_resubmit).props("unelevated no-caps")
    else:
        ui.label("答案已反哺入库，正在验证能否重新命中…").classes("text-info text-caption")
        if t.get("answer"):
            with ui.expansion("查看已提交答案", icon="description").classes("w-full"):
                ui.label(t["answer"]).classes("text-caption whitespace-pre-wrap")

    async def _reverify() -> None:
        ret = await asyncio.to_thread(api.post, f"/tickets/{tid}/reverify", json={})
        if ret is not None and ret.json().get("code") == 200:
            ui.notify("已重新验证", type="positive")
            await refresh_cb()
        else:
            msg = ret.json().get("msg", "验证失败") if ret is not None else "验证失败"
            ui.notify(msg, type="negative")

    async def _close() -> None:
        ret = await asyncio.to_thread(api.post, f"/tickets/{tid}/close", json={})
        if ret is not None and ret.json().get("code") == 200:
            ui.notify(f"工单 #{tid} 已关闭", type="positive")
            await refresh_cb()
        else:
            msg = ret.json().get("msg", "关闭失败") if ret is not None else "关闭失败"
            ui.notify(msg, type="negative")

    with ui.row().classes("w-full gap-2"):
        ui.button("重新验证", on_click=_reverify).props("outline no-caps")
        ui.button("关闭归档", on_click=_close).props("outline no-caps")


def _render_delete(
    api: ApiRequest, tid: int, refresh_cb: Callable[[], Awaitable[None]]
) -> None:
    """两段确认删除：dialog 二次确认（避免误删）。"""

    async def _do_delete() -> None:
        ret = await asyncio.to_thread(api.delete, f"/tickets/{tid}")
        if ret is not None and ret.json().get("code") == 200:
            ui.notify(f"工单 #{tid} 已删除", type="positive")
            await refresh_cb()
        else:
            msg = ret.json().get("msg", "删除失败") if ret is not None else "删除失败"
            ui.notify(msg, type="negative")

    def _open_dialog() -> None:
        with ui.dialog() as dialog, ui.card().classes("gap-3"):
            ui.label(f"确认删除工单 #{tid}？").classes("text-subtitle1 font-medium")
            ui.label("删除后不可恢复；若该工单已反哺知识库，会同步清理对应内容。").classes(
                "text-caption text-warning"
            )
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=dialog.close).props("flat no-caps")
                ui.button(
                    "确认删除",
                    on_click=lambda: (dialog.close(), asyncio.create_task(_do_delete())),
                ).props("color=negative unelevated no-caps")
        dialog.open()

    with ui.row().classes("w-full justify-end pt-2"):
        ui.button(f"🗑 删除工单 #{tid}", on_click=_open_dialog).props(
            "flat no-caps color=negative dense"
        )
