"""知识库管理调试页（NiceGUI）：文件入库 / 检索检查 / 入库审计。

定位：8501 作为后台调试入口，本页承载最核心的「文件入库」能力，
等价于旧 Streamlit 版 knowledge_base/knowledge_base.py 的职责，
按 NiceGUI 事件驱动模型重写（无 rerun、无 session_state、无后台线程快照）。

面板：
1. 知识库管理：选择/新建 KB、上传入库（带进度）、文件清单（移除索引/重建/下载），
   简介与删除类不可逆操作收进「知识库信息 · 危险操作」折叠区；系统托管库只读。
2. 文档检索检查：query + top_k + 阈值 → 命中片段表（调试入库/检索质量用）
3. 入库审计：跑 run_ingestion_audit()（snippet 包含率 + 子块哨兵），结果渲染在页内
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nicegui import ui

from kylin.adapters.inbound.webui.utils import ApiRequest
from kylin.platform.config.settings import Settings
from kylin.platform.runtime.net import api_address

logger = logging.getLogger(__name__)

APP_NAME = "知识库管理 · 调试台"

KB_CSS = """
<style>
.kb-drawer { background: #15202b !important; color: #e8eef2; }
.kb-drawer .q-field__native, .kb-drawer .q-field__label { color: #c5d0da !important; }
.kb-drawer .q-field__control { background: #1e2c3a !important; }
.kb-card { border: 1px solid #e3e9f0; border-radius: 12px; padding: 16px 18px; background: #fff; }
.kb-hit { border-left: 3px solid #2f7f8a; background: #f7fafb; border-radius: 0 8px 8px 0;
  padding: 8px 10px; margin-bottom: 8px; }
.kb-hit .src { font-size: 13px; font-weight: 500; color: #15202b; word-break: break-all; }
.kb-hit .body { font-size: 12px; color: #4a5b6a; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
</style>
"""


def _caption(text: str):
    """NiceGUI 1.4.x 无 ui.caption，用 text-caption 小标签兜底。"""
    return ui.label(text).classes("text-caption")


def _failed_files_from_response(ret: Any) -> Dict[str, str]:
    """从 upload/update 接口响应里解析失败文件列表（沿用旧页面语义）。"""
    if not isinstance(ret, dict):
        return {"": "API 返回格式异常"}
    data = ret.get("data")
    if isinstance(data, dict):
        failed = data.get("failed_files") or {}
        if failed:
            return {str(k): str(v) for k, v in failed.items()}
    if ret.get("code") not in (None, 200):
        return {"": str(ret.get("msg") or "请求失败")}
    return {}


async def _load_kb_list(api: ApiRequest, timeout: float = 2.0) -> List[Dict]:
    """KB 列表（含 kb_info / managed 等元信息）。

    timeout 用于防 reload 冷启动：uvicorn 热重载后首次 render 时 7861 可能尚未就绪，
    asyncio.to_thread 无法响应取消，不加 wait_for 会挂到 httpx 默认超时（数十秒），
    直接撞 nicegui 客户端 3s「Response not ready」超时报 500。超时降级为空列表，
    页面正常渲染空下拉，用户点「刷新」即可恢复。
    """
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(api.list_knowledge_bases), timeout=timeout
        )
    except Exception:  # noqa: BLE001
        return []
    return [x for x in (raw or []) if isinstance(x, dict) and x.get("kb_name")]


async def _list_files(api: ApiRequest, kb: str, timeout: float = 2.0) -> List[Dict]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(api.list_kb_docs, kb), timeout=timeout)
    except Exception:  # noqa: BLE001
        return []


def register_pages() -> None:
    @ui.page("/kb")
    async def kb_page() -> None:  # noqa: ANN202
        ui.add_head_html(KB_CSS)
        ui.colors(primary="#2f7f8a")

        api = ApiRequest(base_url=api_address())
        kb_list = await _load_kb_list(api)
        kb_names = [x["kb_name"] for x in kb_list]
        kb_meta = {x["kb_name"]: x for x in kb_list}
        def _is_managed(kb: str) -> bool:
            """是否系统托管库（OA 同步 / 工单反哺）——服务端只读，UI 需同步禁写。"""
            return bool((kb_meta.get(kb) or {}).get("managed"))

        # 默认选中「非托管」业务库：优先 Settings 默认库名，其次第一个非托管库，
        # 避免默认落在只读的 OA/工单托管库上（旧硬编码 oa_ma 为历史遗留，已废弃）。
        _default_name = Settings.kb_settings.DEFAULT_KNOWLEDGE_BASE
        default_kb = (
            _default_name
            if _default_name in kb_names and not _is_managed(_default_name)
            else next(
                (x["kb_name"] for x in kb_list if not x.get("managed")),
                (kb_names[0] if kb_names else ""),
            )
        )

        # ---- 页头导航 ----
        with ui.row().classes("w-full max-w-[1400px] mx-auto items-baseline gap-4 pt-4"):
            ui.label("知识库管理").classes("text-h5 font-medium")
            ui.link("工单 →", "/").classes("text-subtitle2")
            _caption("文件入库 · 检索检查 · 入库审计 · 后端 :7861")

        with ui.column().classes("w-full max-w-[1400px] mx-auto gap-4 pb-8"):

            # ================= Panel 1: KB 管理 =================
            with ui.card().classes("w-full kb-card"):
                ui.label("① 知识库管理").classes("text-subtitle1 font-medium")
                ui.separator()

                # 系统托管库（OA 同步/工单反哺）只读提示；managed 由后端 list 接口返回
                readonly_banner = ui.label("").classes("text-caption text-warning")
                readonly_banner.set_visibility(False)

                # 当前库文件详情快照（含 in_db/in_folder 布尔），供上传查重与按钮可用性判断
                files_state: List[Dict] = []

                # ---- 选择 / 新建 ----
                with ui.row().classes("w-full items-center gap-3"):
                    kb_sel = ui.select(
                        options=kb_names, value=default_kb or None, label="当前知识库"
                    ).classes("min-w-[220px]")
                    new_kb_in = ui.input("新建 KB 名称", placeholder="例如 oa_new").props(
                        "dense outline"
                    ).classes("min-w-[180px]")

                    def _reload_kb(select=None):
                        async def _go():
                            nonlocal kb_names
                            updated = await _load_kb_list(api)
                            kb_names = [x["kb_name"] for x in updated]
                            kb_meta.clear()
                            kb_meta.update({x["kb_name"]: x for x in updated})
                            sel = select or kb_sel
                            sel.options = kb_names
                            if sel.value not in kb_names:
                                sel.value = kb_names[0] if kb_names else None
                            ui.notify("已刷新知识库列表")

                        return _go

                    ui.button("刷新", on_click=_reload_kb()).props("outline dense no-caps")

                    async def _create_kb():
                        name = (new_kb_in.value or "").strip()
                        if not name:
                            ui.notify("请输入知识库名称", type="warning")
                            return
                        if _is_managed(name):
                            ui.notify(
                                f"「{name}」是系统托管库（OA/工单），禁止占名新建，"
                                "请使用独立业务库名（如 oa_manuals）",
                                type="negative",
                            )
                            return
                        ret = await asyncio.to_thread(api.create_knowledge_base, name)
                        err = _failed_files_from_response(ret) if isinstance(ret, dict) else {}
                        if ret and isinstance(ret, dict) and ret.get("code") in (None, 200) and not err:
                            ui.notify(f"已创建知识库：{name}", type="positive")
                            await _reload_kb(select=kb_sel)()
                        else:
                            ui.notify(f"创建失败：{err or ret}", type="negative")

                    ui.button("新建", on_click=_create_kb).props("unelevated no-caps dense")

                # ---- 折叠区：知识库信息 + 危险操作（不可逆，默认折叠防误触） ----
                with ui.expansion("知识库信息 · 危险操作", icon="warning").classes("w-full"):
                    # 简介
                    kb_info_in = ui.textarea("知识库简介", placeholder="一句话描述该知识库").props(
                        "outlined dense autogrow"
                    ).classes("w-full")

                    def _fill_info():
                        meta = kb_meta.get(kb_sel.value) or {}
                        kb_info_in.value = meta.get("kb_info") or ""

                    async def _save_info():
                        ret = await asyncio.to_thread(api.update_kb_info, kb_sel.value, kb_info_in.value or "")
                        if isinstance(ret, dict) and ret.get("code") not in (None, 200):
                            ui.notify(f"保存失败：{ret.get('msg')}", type="negative")
                        else:
                            ui.notify("已保存简介", type="positive")

                    info_save_btn = ui.button("保存简介", on_click=_save_info).props("outline dense no-caps")

                    ui.separator()

                    # 删除知识库（两段确认；托管库禁止删除）
                    delete_armed = {"kb": None}
                    # nicegui 1.4.x 的 set_visibility() 返回 None，必须先拿引用再单独调用
                    del_warn = ui.label("").classes("text-negative text-caption")
                    del_warn.set_visibility(False)

                    def _arm_delete():
                        kb = kb_sel.value
                        if not kb:
                            ui.notify("请先选择知识库", type="warning")
                            return
                        if _is_managed(kb):
                            ui.notify("系统托管库（OA/工单）禁止删除", type="negative")
                            return
                        delete_armed["kb"] = kb
                        del_warn.set_text(f"确认删除知识库「{kb}」？源文件与索引将一并清除，不可恢复。")
                        del_warn.set_visibility(True)
                        del_btns.set_visibility(True)

                    async def _do_delete():
                        kb = delete_armed["kb"]
                        delete_armed["kb"] = None
                        del_warn.set_visibility(False)
                        del_btns.set_visibility(False)
                        if not kb:
                            return
                        ret = await asyncio.to_thread(api.delete_knowledge_base, kb)
                        if isinstance(ret, dict) and ret.get("code") not in (None, 200):
                            ui.notify(f"删除失败：{ret.get('msg')}", type="negative")
                        else:
                            ui.notify(f"已删除：{kb}", type="positive")
                            await _reload_kb(select=kb_sel)()

                    def _cancel_delete():
                        delete_armed["kb"] = None
                        del_warn.set_visibility(False)
                        del_btns.set_visibility(False)

                    arm_delete_btn = ui.button("删除知识库", on_click=_arm_delete).props(
                        "outline dense no-caps color=negative"
                    )
                    with ui.row().classes("items-center gap-2") as del_btns:
                        ui.button("确认删除", on_click=_do_delete).props("unelevated dense color=negative")
                        ui.button("取消", on_click=_cancel_delete).props("outline dense no-caps")
                    del_btns.set_visibility(False)

                    ui.separator()

                    # 删除文件（含源文件）：不可逆，必须两段确认
                    file_del_armed: List[str] = []

                    def _arm_delete_files():
                        names = list(file_sel.value or [])
                        if not names:
                            ui.notify("请先选择要删除的文件", type="warning")
                            return
                        if _is_managed(kb_sel.value):
                            ui.notify("系统托管库禁止删除文件", type="negative")
                            return
                        file_del_armed[:] = names
                        file_del_warn.set_text(
                            f"确认删除 {len(names)} 个文件的【源文件】？磁盘文件将不可恢复。"
                            "仅想下架（保留源文件）请用文件清单下方的「移除索引」。"
                        )
                        file_del_warn.set_visibility(True)
                        file_del_btns.set_visibility(True)

                    async def _do_delete_files():
                        names = list(file_del_armed)
                        file_del_armed.clear()
                        file_del_warn.set_visibility(False)
                        file_del_btns.set_visibility(False)
                        if not names:
                            return
                        ret = await asyncio.to_thread(api.delete_kb_docs, kb_sel.value, names, True)
                        if isinstance(ret, dict) and ret.get("code") not in (None, 200):
                            ui.notify(f"删除失败：{ret.get('msg')}", type="negative")
                        else:
                            ui.notify(f"已删除 {len(names)} 个文件（含源文件）", type="positive")
                        file_sel.value = []
                        await _render_files()

                    def _cancel_delete_files():
                        file_del_armed.clear()
                        file_del_warn.set_visibility(False)
                        file_del_btns.set_visibility(False)

                    file_del_warn = ui.label("").classes("text-negative text-caption")
                    file_del_warn.set_visibility(False)
                    _caption("删除文件（含源文件）前，请先在下方文件清单勾选目标文件。")
                    del_src_btn = ui.button("删除文件(含源文件)", on_click=_arm_delete_files).props(
                        "outline dense no-caps color=negative"
                    )
                    with ui.row().classes("items-center gap-2") as file_del_btns:
                        ui.button("确认删除源文件", on_click=_do_delete_files).props("unelevated dense color=negative")
                        ui.button("取消", on_click=_cancel_delete_files).props("outline dense no-caps")
                    file_del_btns.set_visibility(False)

                ui.separator()

                # ---- 上传入库 ----
                pending_uploads: List[Tuple[str, bytes]] = []

                def _on_multi_upload(e):  # noqa: ANN001
                    names = list(e.names or [])
                    contents = list(e.contents or [])
                    for i, name in enumerate(names):
                        pending_uploads.append((os.path.basename(name), contents[i] if i < len(contents) else b""))
                    up_status.set_text(f"待入库 {len(pending_uploads)} 个文件")

                up_status = ui.label("尚未选择文件").classes("text-caption")
                upload_widget = ui.upload(multiple=True, on_multi_upload=_on_multi_upload, auto_upload=False).classes("w-full")

                async def _do_upload():
                    kb = kb_sel.value
                    if not kb:
                        ui.notify("请先选择知识库", type="warning")
                        return
                    if not pending_uploads:
                        ui.notify("请先选择要上传的文件", type="warning")
                        return
                    existing = {f.get("file_name") for f in files_state}
                    dup = [n for n, _ in pending_uploads if n in existing]
                    if dup:
                        status.set_text(f"注意：{len(dup)} 个文件与库内同名，将覆盖更新源文件并重建索引")
                    total = len(pending_uploads)
                    failed: List[str] = []
                    tmpdir = tempfile.mkdtemp()
                    try:
                        for i, (name, content) in enumerate(pending_uploads, 1):
                            progress.value = (i - 1) / total
                            status.set_text(f"({i}/{total}) 上传入库: {name}")
                            dest = os.path.join(tmpdir, name)
                            with open(dest, "wb") as fh:
                                fh.write(content)
                            try:
                                ret = await asyncio.to_thread(
                                    api.upload_kb_docs, [dest], kb, True
                                )
                                if _failed_files_from_response(ret):
                                    failed.append(name)
                            finally:
                                with contextlib.suppress(OSError):
                                    os.remove(dest)
                            progress.value = i / total
                    finally:
                        with contextlib.suppress(OSError):
                            os.rmdir(tmpdir)
                    pending_uploads.clear()
                    up_status.set_text("尚未选择文件")
                    progress.value = 0
                    if not failed:
                        status.set_text(f"完成：{total} 个文件全部入库")
                        ui.notify(f"已入库 {total} 个文件", type="positive")
                    else:
                        status.set_text(
                            f"完成 {total - len(failed)} 个，失败 {len(failed)} 个：{', '.join(failed)}"
                        )
                        ui.notify(
                            f"部分失败：{', '.join(failed)}。源文件已保留，"
                            "可在下方勾选后用「重建索引(所选)」补跑",
                            type="warning",
                        )
                    await _render_files()

                upload_btn = ui.button("开始入库", on_click=_do_upload).props("unelevated no-caps")

                # 进度
                progress = ui.linear_progress(value=0).classes("w-full")
                status = ui.label("").classes("text-caption")

                ui.separator()

                # ---- 文件清单 + 操作 ----
                ui.label("文件清单").classes("text-subtitle2")
                files_table = ui.table(
                    columns=[
                        {"name": "file_name", "label": "文件名", "field": "file_name", "align": "left"},
                        {"name": "docs_count", "label": "块数", "field": "docs_count", "align": "left"},
                        {"name": "in_folder", "label": "源文件", "field": "in_folder", "align": "left"},
                        {"name": "in_db", "label": "已入库", "field": "in_db", "align": "left"},
                    ],
                    rows=[],
                ).classes("w-full")

                file_sel = ui.select([], multiple=True, label="选择文件执行操作").classes("w-full")

                # 请求序号：快速切换 KB 时丢弃旧库的晚到响应，避免覆盖新列表
                files_seq = {"n": 0}

                async def _render_files():
                    nonlocal files_state
                    files_seq["n"] += 1
                    my_seq = files_seq["n"]
                    files = await _list_files(api, kb_sel.value)
                    if my_seq != files_seq["n"]:
                        return  # 已有更新请求（切库/再次刷新），丢弃本次响应
                    files_state = list(files or [])
                    rows = []
                    for i, f in enumerate(files or [], 1):
                        rows.append({
                            "file_name": f.get("file_name"),
                            "docs_count": f.get("docs_count"),
                            "in_folder": "✓" if f.get("in_folder") else "—",
                            "in_db": "✓" if f.get("in_db") else "—",
                        })
                    files_table.rows = rows
                    file_sel.options = [f.get("file_name") for f in (files or []) if f.get("file_name")]
                    _update_file_buttons()

                def _selected_files() -> List[Dict]:
                    chosen = set(file_sel.value or [])
                    return [f for f in files_state if f.get("file_name") in chosen]

                def _update_file_buttons():
                    """按「托管只读 + 所选文件状态」刷新文件操作按钮可用性。"""
                    selected = _selected_files()
                    has_selection = bool(selected)
                    has_indexed = any(bool(f.get("in_db")) for f in selected)
                    readonly = _is_managed(kb_sel.value)
                    _set_enabled(remove_idx_btn, not readonly and has_indexed)
                    _set_enabled(rebuild_sel_btn, not readonly and has_selection)
                    _set_enabled(rebuild_all_btn, not readonly)
                    _set_enabled(del_src_btn, not readonly and has_selection)
                    # 下载对托管库也开放（只读允许查看/下载）
                    _set_enabled(download_btn, has_selection)

                def _on_kb_change(e):  # noqa: ANN001
                    file_sel.value = []
                    _apply_readonly_state()
                    asyncio.create_task(_render_files())
                    _fill_info()

                kb_sel.on_value_change(_on_kb_change)
                # 选择文件变化时刷新文件操作按钮可用性（含托管只读态）
                file_sel.on_value_change(lambda e: _update_file_buttons())

                async def _remove_index_selected():
                    """仅移除索引/台账（delete_content=False），保留磁盘源文件，可随时重新入库。"""
                    names = list(file_sel.value or [])
                    if not names:
                        ui.notify("请先选择要移除索引的文件", type="warning")
                        return
                    ret = await asyncio.to_thread(api.delete_kb_docs, kb_sel.value, names, False)
                    if isinstance(ret, dict) and ret.get("code") not in (None, 200):
                        ui.notify(f"移除索引失败：{ret.get('msg')}", type="negative")
                    else:
                        ui.notify(f"已移除 {len(names)} 个文件的索引（源文件已保留）", type="positive")
                    file_sel.value = []
                    await _render_files()

                # 幂等重建：update_docs 内部先删旧索引再写（upsert），失败只伤单文件且可重试
                async def _rebuild_files(names: List[str], *, what: str):
                    total = len(names)
                    failed: List[str] = []
                    for i, fn in enumerate(names, 1):
                        progress.value = (i - 1) / total
                        status.set_text(f"({i}/{total}) {what}: {fn}")
                        ret = await asyncio.to_thread(api.update_kb_docs, kb_sel.value, [fn])
                        if _failed_files_from_response(ret):
                            failed.append(fn)
                        progress.value = i / total
                    progress.value = 0
                    if not failed:
                        status.set_text(f"{what}完成：{total} 个文件")
                        ui.notify(f"已{what} {total} 个文件", type="positive")
                    else:
                        status.set_text(f"{what}完成：{total - len(failed)}/{total}，失败 {len(failed)} 个")
                        ui.notify(
                            f"{what}失败 {len(failed)} 个：{', '.join(failed)}；源文件已保留，可重试",
                            type="warning",
                        )
                    await _render_files()

                async def _rebuild_selected():
                    names = list(file_sel.value or [])
                    if not names:
                        ui.notify("请先选择要重建索引的文件", type="warning")
                        return
                    await _rebuild_files(names, what="重建索引")

                async def _rebuild_all_sources():
                    kb = kb_sel.value
                    files = await _list_files(api, kb)
                    source = [f["file_name"] for f in files if f.get("in_folder")]
                    orphan = [f["file_name"] for f in files if f.get("in_db") and not f.get("in_folder")]
                    if not source:
                        ui.notify("没有可重建的源文件", type="warning")
                        return
                    if orphan:
                        status.set_text(f"清理 {len(orphan)} 个无源文件的孤儿索引…")
                        await asyncio.to_thread(api.delete_kb_docs, kb, orphan)
                    await _rebuild_files(source, what="重建索引")

                async def _download_selected():
                    names = list(file_sel.value or [])
                    if not names:
                        ui.notify("请先选择要下载的文件", type="warning")
                        return
                    data = await asyncio.to_thread(api.download_kb_doc, kb_sel.value, names[0])
                    if data:
                        if len(names) > 1:
                            ui.notify(f"已选 {len(names)} 个文件，仅下载第一个：{names[0]}（浏览器不支持连发多文件下载）", type="warning")
                        ui.download(data, names[0])
                    else:
                        ui.notify("下载失败（文件不存在）", type="negative")

                with ui.row().classes("gap-2 flex-wrap"):
                    remove_idx_btn = ui.button(
                        "移除索引", on_click=lambda: asyncio.create_task(_remove_index_selected())
                    ).props("outline dense no-caps")
                    rebuild_sel_btn = ui.button(
                        "重建索引(所选)", on_click=lambda: asyncio.create_task(_rebuild_selected())
                    ).props("outline dense no-caps")
                    rebuild_all_btn = ui.button(
                        "重建索引(全部源文件)", on_click=lambda: asyncio.create_task(_rebuild_all_sources())
                    ).props("outline dense no-caps")
                    download_btn = ui.button(
                        "下载所选", on_click=lambda: asyncio.create_task(_download_selected())
                    ).props("outline dense no-caps")

                # ---- 托管库只读控制：写操作统一禁用；只读操作（下载/检索/审计）保留 ----
                def _set_enabled(el, enabled: bool) -> None:
                    if enabled:
                        el.props(remove="disabled")
                    else:
                        el.props("disabled")

                def _apply_readonly_state() -> None:
                    managed = _is_managed(kb_sel.value)
                    readonly_banner.set_text(
                        "🔒 系统托管库（OA 同步 / 工单反哺）：服务端只读。"
                        "可查看 / 下载 / 检索 / 审计；上传、删除、重建、改简介等写操作不可用。"
                    )
                    readonly_banner.set_visibility(managed)
                    for el in (arm_delete_btn, info_save_btn, upload_widget, upload_btn, del_src_btn):
                        _set_enabled(el, not managed)
                    _update_file_buttons()

            # ================= Panel 2: 检索检查 =================
            with ui.card().classes("w-full kb-card"):
                ui.label("② 文档检索检查").classes("text-subtitle1 font-medium")
                ui.separator()
                with ui.row().classes("w-full items-end gap-3 flex-wrap"):
                    q_in = ui.input("问题/关键词", placeholder="输入要检索的内容").classes("min-w-[280px]")
                    topk_in = ui.number("top_k", value=Settings.kb_settings.VECTOR_SEARCH_TOP_K, min=1, max=50).props("dense outline")
                    thr_in = ui.number(
                        "score阈值(距离,越小越严,2=不筛)",
                        value=2.0, min=0.0, max=2.0, step=0.05,
                    ).props("dense outline")
                    fname_in = ui.input("文件名过滤(可选)").props("dense outline")
                    search_btn = ui.button("检索", on_click=lambda: asyncio.create_task(_do_search())).props("unelevated no-caps")

                search_result = ui.column().classes("w-full gap-1")

                async def _do_search():
                    query = (q_in.value or "").strip()
                    if not query:
                        ui.notify("请输入检索内容", type="warning")
                        return
                    # top_k 清空时回退默认值，避免把 0 传给后端导致恒空结果
                    resolved_topk = int(topk_in.value) if topk_in.value else Settings.kb_settings.VECTOR_SEARCH_TOP_K
                    ret = await asyncio.to_thread(
                        api.search_kb_docs,
                        kb_sel.value,
                        query,
                        max(1, resolved_topk),
                        float(thr_in.value if thr_in.value is not None else 2.0),
                        (fname_in.value or "").strip(),
                    )
                    search_result.clear()
                    with search_result:
                        if not ret:
                            ui.label("无命中（阈值过严或该问题确实不在库中）").classes("text-caption text-grey-7")
                            return
                        for i, d in enumerate(ret or [], 1):
                            meta = d.get("metadata") or {}
                            src = meta.get("source") or meta.get("title_path") or d.get("id") or "参考"
                            # rerank 正常路径下 score=relevance（0-1 相关度）；优先取 relevance_score，
                            # 与 KBSettings.RERANK_SCORE_THRESHOLD 同一口径
                            score = meta.get("relevance_score", meta.get("score"))
                            score_txt = f"rel {float(score):.2f}" if score is not None else ""
                            body = (d.get("page_content") or "").strip()
                            with ui.column().classes("kb-hit w-full gap-1"):
                                with ui.row().classes("w-full items-center gap-2"):
                                    ui.badge(f"[{i}]", color="teal-7")
                                    ui.label(str(src)).classes("src")
                                    if score_txt:
                                        ui.badge(score_txt, color="teal-2").props("text-color=teal-9")
                                if len(body) > 200:
                                    with ui.expansion(f"展开原文（{len(body)} 字）").classes("w-full"):
                                        ui.label(body).classes("body")
                                else:
                                    ui.label(body).classes("body")

            # ================= Panel 3: 入库审计 =================
            with ui.card().classes("w-full kb-card"):
                ui.label("③ 入库审计").classes("text-subtitle1 font-medium")
                ui.separator()
                _caption("snippet 包含率（解析/切块质量）+ 子块统计哨兵；结果只读 ES，不重解析。")
                audit_btn = ui.button("运行审计", on_click=lambda: asyncio.create_task(_do_audit())).props("unelevated no-caps")
                audit_status = ui.label("").classes("text-caption")
                audit_result = ui.column().classes("w-full gap-1")

                async def _do_audit():
                    kb = kb_sel.value
                    audit_btn.disable()
                    audit_status.set_text(f"审计中（kb={kb}）…")
                    try:
                        def _work():
                            from kylin.adapters.inbound.jobs.eval.ingestion_audit import run_ingestion_audit

                            # 产物写临时目录用完即删：不污染 data/eval/results 的正式对比报告产物，
                            # 也不像旧实现 mkdtemp 那样每次审计泄漏一个目录
                            with tempfile.TemporaryDirectory() as tmp:
                                return run_ingestion_audit(kb_name=kb, out_dir=Path(tmp))

                        payload = await asyncio.to_thread(_work)
                    except Exception as exc:  # noqa: BLE001
                        audit_status.set_text(f"审计失败：{exc}")
                        audit_btn.enable()
                        return
                    audit_btn.enable()
                    audit_status.set_text("")
                    c = payload.get("containment") or {}
                    s = payload.get("chunk_stats") or {}
                    judged = c.get("n_contained", 0) + c.get("n_missing", 0)
                    eval_file = str(payload.get("eval_file") or "")
                    audit_result.clear()
                    with audit_result:
                        _caption(f"金标集：{eval_file or '（默认 data/eval 金标）'} · 包含率仅对与金标同源的库有意义")
                        if judged == 0:
                            ui.label(
                                "金标集与本库不匹配（无可判定金标，父块缺失 "
                                f"{c.get('n_parent_missing', 0)}），包含率不可用；下方 chunk 统计仍有效。"
                            ).classes("text-caption text-warning")
                        else:
                            ui.label(
                                f"snippet 包含率：{c.get('n_contained', 0)}/{judged}"
                                f"（{c.get('containment_rate')}）· 父块缺失 {c.get('n_parent_missing', 0)}"
                            ).classes("font-medium")
                        ui.label(
                            f"子块：{s.get('total_children')} 个 / 父块 {s.get('total_parents')} 个"
                            f"· 空块 {s.get('empty_children')} · 超长块 {s.get('overlong_children')}"
                            f"· 平均 {s.get('chars', {}).get('avg')} 字符"
                        ).classes("text-caption")
                        for w in s.get("sentinels") or []:
                            ui.label(f"[{w.get('level')}] {w.get('name')} ×{w.get('count')}：{w.get('hint')}").classes(
                                "text-caption text-warning"
                            )
                        failures = c.get("failures") or []
                        if failures:
                            ui.label(f"未包含片段 {len(failures)} 条：").classes("text-subtitle2 mt-2")
                            for f in failures[:20]:
                                ui.label(
                                    f"· {f.get('qid')} {f.get('query')} → {f.get('reason')}"
                                ).classes("text-caption")

        # 初始渲染（KB 列表拉取失败时跳过文件清单，避免冷启动叠加超时）
        _fill_info()
        _apply_readonly_state()
        if kb_names:
            await _render_files()
