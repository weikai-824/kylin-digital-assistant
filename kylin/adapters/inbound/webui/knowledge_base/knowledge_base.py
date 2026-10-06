import json
import time
from contextlib import contextmanager
from threading import Thread
from typing import Callable, Dict, List, Literal, Tuple, TypeVar

import pandas as pd
import streamlit as st
import streamlit_antd_components as sac
from st_aggrid import AgGrid, JsCode
from st_aggrid.grid_options_builder import GridOptionsBuilder
from streamlit_antd_components.utils import ParseItems

from kylin.adapters.inbound.webui.utils import (
    ApiRequest,
    check_error_msg,
    check_success_msg,
)
from kylin.adapters.outbound.model.clients import get_config_models, get_default_embedding
from kylin.ports.data.ingestion import SUPPORTED_INGESTION_FILE_EXTENSIONS
from kylin.platform.config.settings import Settings

# SENTENCE_SIZE = 100

cell_renderer = JsCode(
    """function(params) {if(params.value==true){return '✓'}else{return '×'}}"""
)

_KB_INGEST_BUSY_KB = "kb_ingest_busy_kb"
_KB_INGEST_BUSY_SINCE = "kb_ingest_busy_since"
_KEEPALIVE_INTERVAL_SECONDS = 5.0
_INGEST_BUSY_STALE_SECONDS = 7200.0

T = TypeVar("T")


def config_aggrid(
    df: pd.DataFrame,
    columns: Dict[Tuple[str, str], Dict] = {},
    selection_mode: Literal["single", "multiple", "disabled"] = "single",
    use_checkbox: bool = False,
) -> GridOptionsBuilder:
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_column("No", width=40)
    for (col, header), kw in columns.items():
        gb.configure_column(col, header, wrapHeaderText=True, **kw)
    gb.configure_selection(
        selection_mode=selection_mode,
        use_checkbox=use_checkbox,
        pre_selected_rows=st.session_state.get("selected_rows", [0]),
    )
    gb.configure_pagination(
        enabled=True, paginationAutoPageSize=False, paginationPageSize=10
    )
    return gb


def selected_source_file(selected_rows: list[dict]) -> str:
    """
    返回当前选中的、仍保留源文件的文件名。
    """
    if selected_rows:
        row = selected_rows[0]
        if row.get("in_folder") in (True, "✓"):
            return str(row["file_name"])
    return ""


def _has_source_file(row: dict) -> bool:
    return row.get("in_folder") in (True, "✓")


def _failed_files_from_response(ret: dict) -> dict[str, str]:
    if not isinstance(ret, dict):
        return {"": "API 返回格式异常"}
    data = ret.get("data")
    if isinstance(data, dict):
        failed = data.get("failed_files") or {}
        if failed:
            return {str(name): str(msg) for name, msg in failed.items()}
    if ret.get("code") not in (None, 200):
        return {"": str(ret.get("msg") or "请求失败")}
    return {}


def _is_kb_ingest_busy(kb: str) -> bool:
    busy_kb = st.session_state.get(_KB_INGEST_BUSY_KB)
    if busy_kb != kb:
        return False
    since = float(st.session_state.get(_KB_INGEST_BUSY_SINCE, 0))
    if time.time() - since > _INGEST_BUSY_STALE_SECONDS:
        st.session_state.pop(_KB_INGEST_BUSY_KB, None)
        st.session_state.pop(_KB_INGEST_BUSY_SINCE, None)
        return False
    return True


@contextmanager
def _kb_ingest_lock(kb: str):
    st.session_state[_KB_INGEST_BUSY_KB] = kb
    st.session_state[_KB_INGEST_BUSY_SINCE] = time.time()
    try:
        yield
    finally:
        st.session_state.pop(_KB_INGEST_BUSY_KB, None)
        st.session_state.pop(_KB_INGEST_BUSY_SINCE, None)


def _run_with_keepalive(
    fn: Callable[[], T],
    *,
    progress,
    ratio: float,
    label: str,
    interval: float = _KEEPALIVE_INTERVAL_SECONDS,
) -> T:
    """后台执行阻塞 API，主线程定期刷新进度，避免 Streamlit WebSocket 空闲超时。"""
    result: dict[str, T] = {}
    error: dict[str, BaseException] = {}

    def worker() -> None:
        try:
            result["value"] = fn()
        except BaseException as exc:
            error["value"] = exc

    thread = Thread(target=worker, daemon=True)
    started = time.monotonic()
    thread.start()
    while thread.is_alive():
        elapsed = int(time.monotonic() - started)
        progress.progress(ratio, f"{label} … 已等待 {elapsed}s")
        thread.join(timeout=interval)
    if "value" in error:
        raise error["value"]
    return result["value"]


def _update_kb_docs_one_by_one(
    api: ApiRequest,
    kb: str,
    file_names: list[str],
    *,
    chunk_size: int,
    chunk_overlap: int,
    progress,
    access_depts: List[str] | None = None,
) -> tuple[int, int, list[str]]:
    """逐文件建立/更新索引，避免一次请求绑定过长 Streamlit 会话。"""
    total = len(file_names)
    failed: list[str] = []
    for index, file_name in enumerate(file_names, start=1):
        ratio = (index - 1) / total if total else 0.0
        label = f"({index}/{total}) 解析入库: {file_name}"
        progress.progress(ratio, label)
        ret = _run_with_keepalive(
            lambda file_name=file_name: api.update_kb_docs(
                kb,
                file_names=[file_name],
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                access_depts=access_depts,
            ),
            progress=progress,
            ratio=ratio,
            label=label,
        )
        file_failed = _failed_files_from_response(ret)
        if file_failed:
            failed.append(file_name)
            progress.progress(
                index / total if total else 1.0,
                f"({index}/{total}) 失败: {file_name}",
            )
        else:
            progress.progress(
                index / total if total else 1.0,
                f"({index}/{total}) 完成: {file_name}",
            )
    success_count = total - len(failed)
    return success_count, len(failed), failed


def _upload_kb_docs_one_by_one(
    api: ApiRequest,
    kb: str,
    files,
    *,
    chunk_size: int,
    chunk_overlap: int,
    progress,
    metadata: Dict = {},
) -> tuple[int, int, list[str]]:
    """逐文件上传并入库，单个文件失败不影响其余文件。"""
    total = len(files)
    failed: list[str] = []
    for index, uploaded in enumerate(files, start=1):
        file_name = uploaded.name
        ratio = (index - 1) / total if total else 0.0
        label = f"({index}/{total}) 上传入库: {file_name}"
        progress.progress(ratio, label)
        ret = _run_with_keepalive(
            lambda uploaded=uploaded: api.upload_kb_docs(
                [uploaded],
                knowledge_base_name=kb,
                override=True,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                metadata=metadata,
            ),
            progress=progress,
            ratio=ratio,
            label=label,
        )
        file_failed = _failed_files_from_response(ret)
        if file_failed:
            failed.append(file_name)
            progress.progress(
                index / total if total else 1.0,
                f"({index}/{total}) 失败: {file_name}",
            )
        else:
            progress.progress(
                index / total if total else 1.0,
                f"({index}/{total}) 完成: {file_name}",
            )
    success_count = total - len(failed)
    return success_count, len(failed), failed


def _render_create_kb(api, kb_list, managed_names: set[str] | None = None) -> None:
    """新建知识库表单与提交。"""
    managed_names = managed_names or set()

    with st.form("新建知识库"):
        kb_name = st.text_input(
            "新建知识库名称",
            placeholder="例如 oa_manuals（勿占用 oa_kb / knowledge_tickets）",
            key="kb_name",
            help="OA 库与工单库为系统托管只读库，不可用同名新建。",
        )
        kb_info = st.text_input(
            "知识库简介",
            placeholder="知识库简介（可选）",
            key="kb_info",
        )

        col0, _ = st.columns([3, 1])

        vs_types = ["dual"]
        vs_type = col0.selectbox(
            "存储类型（仅 dual = Milvus + ES）",
            vs_types,
            index=0,
            key="vs_type",
        )

        col1, _ = st.columns([3, 1])
        with col1:
            embed_models = list(get_config_models(model_type="embed"))
            index = 0
            if get_default_embedding() in embed_models:
                index = embed_models.index(get_default_embedding())
            embed_model = st.selectbox("Embeddings模型", embed_models, index)

        submit_create_kb = st.form_submit_button(
            "新建",
            use_container_width=True,
        )

    if submit_create_kb:
        name = (kb_name or "").strip()
        if not name:
            st.error("知识库名称不能为空！")
        elif name in managed_names:
            st.error(
                f"「{name}」是系统托管库（OA / 工单），可查看不可占名创建；"
                "请使用独立业务库名（如 oa_manuals）。"
            )
        elif name in kb_list:
            st.error(f"名为 {name} 的知识库已经存在！")
        elif embed_model is None:
            st.error("请选择Embedding模型！")
        else:
            ret = api.create_knowledge_base(
                knowledge_base_name=name,
                vector_store_type=vs_type,
                embed_model=embed_model,
            )
            st.toast(ret.get("msg", " "))
            if ret.get("code") == 200:
                st.session_state["selected_kb_name"] = name
                st.session_state["selected_kb_info"] = kb_info
            st.rerun()


def _render_files_section(
    api,
    kb,
    kb_list,
    *,
    readonly: bool = False,
    managed: bool = False,
    content_editable: bool = True,
):
    """上传配置 + 文件表格 + 索引/删除操作。返回 (chunk_size, chunk_overlap, selected_rows)。

    readonly：会话解锁锁（False 才可操作可见范围等）。
    content_editable：是否可改内容/上传/改简介/重建（托管库解锁后仍为 False，仅可改可见范围）。
    """

    st.session_state["selected_kb_info"] = kb_list[kb]["kb_info"]
    if managed:
        st.info(
            "该知识库为系统托管（OA 同步 / 工单反哺）：可查看与下载文件。"
            "解锁后仅可修改可见范围，不可上传、改简介或删改索引。"
        )
    elif readonly:
        st.caption("🔒 只读保护中：可查看与下载，编辑操作已锁定。")

    # 上传文件（托管库即使解锁也不允许上传）
    files = st.file_uploader(
        "上传知识文件：",
        SUPPORTED_INGESTION_FILE_EXTENSIONS,
        accept_multiple_files=True,
        disabled=not content_editable,
    )

    # 可见范围：默认全开放；可选"仅指定部门"（WebUI 为管理员页面，解锁后可设）
    upload_metadata: Dict = {}
    if content_editable:
        scope = st.radio(
            "上传文档可见范围",
            ["全开放", "仅指定部门"],
            index=0,
            horizontal=True,
            key=f"upload_scope_{kb}",
            help="全开放 = 所有部门可见；仅指定部门 = 只有勾选的部门可见。默认全开放。",
        )
        if scope == "仅指定部门":
            depts_text = st.text_input(
                "可见部门 ID（分号或逗号分隔，如 D1;D2）",
                key=f"upload_depts_{kb}",
            )
            dept_list = [
                d.strip()
                for d in depts_text.replace("，", ",").replace(";", ",").split(",")
                if d.strip()
            ]
            if dept_list:
                upload_metadata["access_depts"] = dept_list
    kb_info = st.text_area(
        "请输入知识库介绍:",
        value=st.session_state["selected_kb_info"],
        max_chars=None,
        key=None,
        help=None,
        on_change=None,
        args=None,
        kwargs=None,
        disabled=not content_editable,
    )

    if content_editable and kb_info != st.session_state["selected_kb_info"]:
        st.session_state["selected_kb_info"] = kb_info
        api.update_kb_info(kb, kb_info)

    with st.expander(
        "文件处理配置",
        expanded=content_editable,
    ):
        cols = st.columns(2)
        chunk_size = cols[0].number_input(
            "子块最大长度：",
            1,
            1000,
            Settings.kb_settings.CHILD_CHUNK_SIZE,
            disabled=not content_editable,
        )
        chunk_overlap = cols[1].number_input(
            "子块重合长度：",
            0,
            chunk_size,
            Settings.kb_settings.CHILD_OVERLAP_SIZE,
            disabled=not content_editable,
        )

    ingest_busy = _is_kb_ingest_busy(kb)
    if ingest_busy:
        st.warning(
            "当前知识库有入库任务进行中（可能在其它浏览器标签页），请等待完成后再操作。"
        )

    if st.button(
        "添加文件到知识库",
        disabled=not content_editable or len(files) == 0 or ingest_busy,
    ):
        with _kb_ingest_lock(kb):
            with st.spinner("文件上传与入库中，大文件请耐心等待…"):
                progress = st.empty()
                progress.progress(0.0, "准备上传…")
                ok, failed_count, failed_names = _upload_kb_docs_one_by_one(
                    api,
                    kb,
                    files,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    progress=progress,
                    metadata=upload_metadata,
                )
        if failed_count == 0:
            st.toast(f"已成功上传并入库 {ok} 个文件", icon="✔")
        elif ok == 0:
            st.toast(f"全部 {failed_count} 个文件失败", icon="✖")
        else:
            st.toast(
                f"完成 {ok} 个，失败 {failed_count} 个：{', '.join(failed_names)}",
                icon="✖",
            )
        st.rerun()

    st.divider()

    # 知识库详情
    doc_details = pd.DataFrame(api.list_kb_docs(kb))
    selected_rows = []
    if not len(doc_details):
        st.info(f"知识库 `{kb}` 中暂无文件")
    else:
        st.write(f"知识库 `{kb}` 中已有文件:")
        st.info(
            "知识库中包含源文件与索引，请从下表中选择文件后操作"
            if not readonly
            else "只读模式：可选择文件下载，不可建立/移除索引或删除文件"
        )
        total = len(doc_details)
        in_db = int(doc_details["in_db"].sum()) if total else 0
        vs_type = kb_list.get(kb, {}).get("vs_type", "")
        st.caption(
            f"索引状态：文件 {total} 个 · 已建索引 {in_db} · 待建索引 {total - in_db}"
            + (f" · 存储类型 {vs_type}" if vs_type else "")
        )
        doc_details.drop(columns=["kb_name"], inplace=True)
        doc_details = doc_details[
            [
                "No",
                "file_name",
                "docs_count",
                "in_folder",
                "in_db",
                "department_id",
                "access_depts",
            ]
        ]
        doc_details["in_folder"] = (
            doc_details["in_folder"].replace(True, "✓").replace(False, "×")
        )
        doc_details["in_db"] = (
            doc_details["in_db"].replace(True, "✓").replace(False, "×")
        )
        # 可见范围：access_depts 空 = 全开放；非空 = 仅指定部门
        doc_details["access_depts"] = doc_details["access_depts"].apply(
            lambda v: (
                "全开放"
                if not v or (isinstance(v, list) and not v)
                else "仅 " + ", ".join(str(x) for x in v)
            )
        )
        # 归属部门：仅展示
        doc_details["department_id"] = (
            doc_details["department_id"].fillna("").astype(str).replace("", "—")
        )
        gb = config_aggrid(
            doc_details,
            {
                ("No", "序号"): {},
                ("file_name", "文档名称"): {},
                ("docs_count", "文档数量"): {},
                ("in_folder", "源文件"): {},
                ("in_db", "已建索引"): {},
                ("access_depts", "可见范围"): {},
                ("department_id", "归属部门ID"): {},
            },
            "multiple",
        )

        doc_grid = AgGrid(
            doc_details,
            gb.build(),
            columns_auto_size_mode="FIT_CONTENTS",
            theme="alpine",
            custom_css={
                "#gridToolBar": {"display": "none"},
            },
            allow_unsafe_jscode=True,
            enable_enterprise_modules=False,
        )

        selected_rows = doc_grid.get("selected_rows")
        if selected_rows is None:
            selected_rows = []
        else:
            selected_rows = selected_rows.to_dict("records")
        cols = st.columns(4)
        file_name = selected_source_file(selected_rows)
        if file_name:
            cols[0].download_button(
                "下载选中文档",
                api.download_kb_doc(kb, file_name),
                file_name=file_name,
                use_container_width=True,
            )
        else:
            cols[0].download_button(
                "下载选中文档",
                "",
                disabled=True,
                use_container_width=True,
            )

        st.write()
        if cols[1].button(
            "重新建立索引"
            if selected_rows and (pd.DataFrame(selected_rows)["in_db"]).any()
            else "建立索引",
            disabled=not content_editable or not selected_source_file(selected_rows) or ingest_busy,
            use_container_width=True,
        ):
            file_names = [
                row["file_name"]
                for row in selected_rows
                if _has_source_file(row)
            ]
            with _kb_ingest_lock(kb):
                with st.spinner("索引建立中，大文件请耐心等待…"):
                    progress = st.empty()
                    progress.progress(0.0, "准备建立索引…")
                    ok, failed_count, failed_names = _update_kb_docs_one_by_one(
                        api,
                        kb,
                        file_names,
                        chunk_size=chunk_size,
                        chunk_overlap=chunk_overlap,
                        progress=progress,
                    )
            if failed_count == 0:
                st.toast(f"已成功建立 {ok} 个文件索引", icon="✔")
            elif ok == 0:
                st.toast(f"全部 {failed_count} 个文件索引失败", icon="✖")
            else:
                st.toast(
                    f"完成 {ok} 个，失败 {failed_count} 个：{', '.join(failed_names)}",
                    icon="✖",
                )
            st.rerun()

        if cols[2].button(
            "移除索引",
            disabled=not content_editable or not (selected_rows and selected_rows[0]["in_db"]),
            use_container_width=True,
        ):
            file_names = [row["file_name"] for row in selected_rows]
            api.delete_kb_docs(kb, file_names=file_names)
            st.rerun()

        if cols[3].button(
            "从知识库中删除",
            type="primary",
            disabled=not content_editable or not selected_rows,
            use_container_width=True,
        ):
            file_names = [row["file_name"] for row in selected_rows]
            api.delete_kb_docs(kb, file_names=file_names, delete_content=True)
            st.rerun()

        # 编辑选中文件的可见范围（解锁后可用；托管库解锁后也可用，仅此操作开放）
        if selected_rows and not readonly:
            st.divider()
            if managed:
                st.caption(
                    "编辑选中文件的可见范围（仅刷权限，不重建内容；系统托管库可用）："
                )
            else:
                st.caption("编辑选中文件的可见范围（重建索引，需一点时间）：")
            target_files = [row["file_name"] for row in selected_rows]
            perm_scope = st.radio(
                "可见范围",
                ["全开放", "仅指定部门"],
                index=0,
                horizontal=True,
                key=f"perm_scope_{kb}_{target_files[0]}",
            )
            perm_depts: List[str] = []
            if perm_scope == "仅指定部门":
                perm_text = st.text_input(
                    "可见部门 ID（分号或逗号分隔）",
                    key=f"perm_depts_{kb}_{target_files[0]}",
                )
                perm_depts = [
                    d.strip()
                    for d in perm_text.replace("，", ",").replace(";", ",").split(",")
                    if d.strip()
                ]
            if st.button(
                "应用可见范围",
                key=f"apply_perm_{kb}_{target_files[0]}",
                disabled=ingest_busy or (perm_scope == "仅指定部门" and not perm_depts),
            ):
                if managed:
                    # 系统托管库走纯刷权限：不重建内容，避免触发重解析/重 embed。
                    with _kb_ingest_lock(kb):
                        with st.spinner("正在更新访问范围…"):
                            failed = set()
                            for fname in target_files:
                                ret = api.update_doc_access_depts(kb, [fname], perm_depts)
                                if _failed_files_from_response(ret):
                                    failed.add(fname)
                    if not failed:
                        st.toast(f"已更新 {len(target_files)} 个文件的可见范围", icon="✔")
                    else:
                        st.toast(f"完成 {len(target_files) - len(failed)} 个，失败 {len(failed)} 个", icon="✖")
                    st.rerun()
                    return chunk_size, chunk_overlap, selected_rows
                with _kb_ingest_lock(kb):
                    with st.spinner("正在重建索引并更新访问范围…"):
                        progress = st.empty()
                        ok, failed_count, failed_names = _update_kb_docs_one_by_one(
                            api,
                            kb,
                            target_files,
                            chunk_size=chunk_size,
                            chunk_overlap=chunk_overlap,
                            progress=progress,
                            access_depts=perm_depts,
                        )
                if failed_count == 0:
                    st.toast(f"已更新 {ok} 个文件的可见范围", icon="✔")
                else:
                    st.toast(
                        f"完成 {ok} 个，失败 {failed_count} 个：{', '.join(failed_names)}",
                        icon="✖",
                    )
                st.rerun()

    return chunk_size, chunk_overlap, selected_rows


def _render_kb_actions(
    api,
    kb,
    chunk_size,
    chunk_overlap,
    *,
    readonly: bool = False,
    managed: bool = False,
    content_editable: bool = True,
) -> None:
    """依据源文件重建索引 / 删除知识库。"""

    st.divider()

    cols = st.columns(3)
    ingest_busy = _is_kb_ingest_busy(kb)

    if cols[0].button(
        "依据源文件重建索引",
        help=(
            "按 content 目录中的源文件逐个重建索引。"
            "若存在未入库文件则仅处理这些文件（可断点续跑）；"
            "若全部已入库则先移除旧索引再完整重建。"
            if content_editable
            else ("系统托管库禁止重建索引" if managed else "只读保护中：需先解锁编辑")
        ),
        use_container_width=True,
        type="primary",
        disabled=not content_editable or ingest_busy,
    ):
        docs = api.list_kb_docs(kb)
        source_files = [row["file_name"] for row in docs if row.get("in_folder")]
        pending_files = [
            row["file_name"]
            for row in docs
            if row.get("in_folder") and not row.get("in_db")
        ]
        indexed_files = [row["file_name"] for row in docs if row.get("in_db")]

        if not source_files:
            st.toast("content 目录下没有可重建的源文件", icon="✖")
        else:
            with _kb_ingest_lock(kb):
                resume_mode = bool(pending_files)
                target_files = pending_files if resume_mode else source_files
                rebuild_ok = True
                with st.spinner(
                    "索引重建中，请耐心等待；每完成一个文件会保存进度…"
                    if resume_mode
                    else "完整重建中：先清空旧索引，再逐文件入库…"
                ):
                    progress = st.empty()
                    if resume_mode:
                        progress.progress(
                            0.0,
                            f"续跑模式：待入库 {len(target_files)} / 共 {len(source_files)} 个源文件",
                        )
                    else:
                        progress.progress(0.0, "准备完整重建索引…")
                        if indexed_files:
                            clear_label = f"移除 {len(indexed_files)} 个文件的旧索引"
                            progress.progress(0.0, clear_label)
                            clear_ret = _run_with_keepalive(
                                lambda: api.delete_kb_docs(
                                    kb, file_names=indexed_files
                                ),
                                progress=progress,
                                ratio=0.0,
                                label=clear_label,
                            )
                            if msg := check_error_msg(clear_ret):
                                st.toast(f"清空旧索引失败：{msg}", icon="✖")
                                rebuild_ok = False
                    if rebuild_ok:
                        ok, failed_count, failed_names = _update_kb_docs_one_by_one(
                            api,
                            kb,
                            target_files,
                            chunk_size=chunk_size,
                            chunk_overlap=chunk_overlap,
                            progress=progress,
                        )
            if rebuild_ok:
                if failed_count == 0:
                    st.toast(f"已重建 {ok} 个文件索引", icon="✔")
                elif ok == 0:
                    st.toast(f"重建失败，共 {failed_count} 个文件", icon="✖")
                else:
                    st.toast(
                        f"完成 {ok} 个，失败 {failed_count} 个：{', '.join(failed_names)}",
                        icon="✖",
                    )
                st.rerun()

    # 删除知识库：两阶段确认（与 tickets_page 删除工单同模式）。
    # 不用 st.popover：popover 在 streamlit 1.34 是位置化状态，
    # 删除后 rerun 会按位置重绑，导致孤儿弹层误删；key 带 kb 唯一化，与渲染位置解耦。
    confirm_key = f"confirm_delete_kb_{kb}"
    if st.session_state.get(confirm_key):
        st.warning("删除知识库将同时删除源文件与全部索引，操作不可恢复！")
        cc1, cc2 = st.columns(2)
        if cc1.button(
            "取消", key=f"cancel_delete_kb_{kb}", use_container_width=True
        ):
            st.session_state.pop(confirm_key, None)
            st.rerun()
        if cc2.button(
            "确认删除",
            key=f"do_delete_kb_{kb}",
            type="primary",
            use_container_width=True,
        ):
            ret = api.delete_knowledge_base(kb)
            st.toast(ret.get("msg", " "))
            st.session_state.pop(confirm_key, None)
            time.sleep(1)
            st.rerun()
    elif cols[2].button(
        "删除知识库",
        use_container_width=True,
        disabled=not content_editable,
        help=("系统托管库禁止删除" if managed else "只读保护中：需先解锁编辑") if not content_editable else None,
    ):
        st.session_state[confirm_key] = True
        st.rerun()


def _render_docs_editor(api, selected_kb, selected_rows, *, readonly: bool = False) -> None:
    """文件内文档列表：双击编辑、删除标记、保存更改。"""

    if readonly:
        st.caption("系统托管库不提供文档片段编辑（只读）。")
        return

    st.write("文件内文档列表。双击进行修改，在删除列填入 Y 可删除对应行。")
    docs = []
    df = pd.DataFrame([], columns=["seq", "id", "content", "source"])
    if selected_rows:
        file_name = selected_rows[0]["file_name"]
        docs = api.search_kb_docs(
            knowledge_base_name=selected_kb, file_name=file_name
        )

        data = [
            {
                "seq": i + 1,
                "id": x.get("id") or "",
                "page_content": x.get("page_content") or "",
                "source": (x.get("metadata") or {}).get("source"),
                # 旧 langchain Document 曾带 type；当前 search_docs 只返回
                # page_content / metadata / id / score，缺省时给空串避免 KeyError。
                "type": x.get("type") or "",
                "metadata": json.dumps(x.get("metadata") or {}, ensure_ascii=False),
                "to_del": "",
            }
            for i, x in enumerate(docs or [])
        ]
        df = pd.DataFrame(data)

        gb = GridOptionsBuilder.from_dataframe(df)
        gb.configure_columns(["id", "source", "type", "metadata"], hide=True)
        gb.configure_column("seq", "No.", width=50)
        gb.configure_column(
            "page_content",
            "内容",
            editable=True,
            autoHeight=True,
            wrapText=True,
            flex=1,
            cellEditor="agLargeTextCellEditor",
            cellEditorPopup=True,
        )
        gb.configure_column(
            "to_del",
            "删除",
            editable=True,
            width=50,
            wrapHeaderText=True,
            cellEditor="agCheckboxCellEditor",
            cellRender="agCheckboxCellRenderer",
        )
        # 启用分页
        gb.configure_pagination(
            enabled=True, paginationAutoPageSize=False, paginationPageSize=10
        )
        gb.configure_selection()
        edit_docs = AgGrid(df, gb.build(), fit_columns_on_grid_load=True)

        if st.button("保存更改"):
            origin_docs = {
                (x.get("id") or ""): {
                    "page_content": x.get("page_content") or "",
                    "type": x.get("type") or "",
                    "metadata": x.get("metadata") or {},
                }
                for x in docs or []
            }
            changed_docs = []
            for index, row in edit_docs.data.iterrows():
                origin_doc = origin_docs.get(row["id"])
                if origin_doc is None:
                    continue
                if row["page_content"] != origin_doc["page_content"]:
                    if row["to_del"] not in ["Y", "y", 1]:
                        changed_docs.append(
                            {
                                "page_content": row["page_content"],
                                "type": row.get("type") or origin_doc.get("type") or "",
                                "metadata": json.loads(row["metadata"]),
                            }
                        )

            if changed_docs:
                if api.update_kb_docs(
                    knowledge_base_name=selected_kb,
                    file_names=[file_name],
                    docs={file_name: changed_docs},
                ):
                    st.toast("更新文档成功")
                else:
                    st.toast("更新文档失败")


def knowledge_base_page(api: ApiRequest, is_lite: bool = None):
    try:
        raw_list = api.list_knowledge_bases() or []
        kb_list = {x["kb_name"]: x for x in raw_list}
    except Exception as e:
        st.error(
            "获取知识库信息错误，请检查是否已按照 `README.md` 中 `4 知识库初始化与迁移` 步骤完成初始化或迁移，或是否为数据库连接错误。"
        )
        st.stop()
    kb_names = list(kb_list.keys())
    managed_names = {
        name for name, item in kb_list.items() if item.get("managed")
    }

    if (
        "selected_kb_name" in st.session_state
        and st.session_state["selected_kb_name"] in kb_names
    ):
        selected_kb_index = kb_names.index(st.session_state["selected_kb_name"])
    else:
        selected_kb_index = 0

    if "selected_kb_info" not in st.session_state:
        st.session_state["selected_kb_info"] = ""

    def format_selected_kb(kb_name: str) -> str:
        if kb := kb_list.get(kb_name):
            label = f"{kb_name} ({kb['vs_type']} @ {kb['embed_model']})"
            if kb.get("managed"):
                return f"{label} · 只读"
            return label
        return kb_name

    selected_kb = st.selectbox(
        "请选择或新建知识库：",
        kb_names + ["新建知识库"],
        format_func=format_selected_kb,
        index=selected_kb_index,
    )

    if selected_kb == "新建知识库":
        _render_create_kb(api, kb_list, managed_names=managed_names)
    elif selected_kb:
        kb = selected_kb
        managed = kb in managed_names
        # 每库编辑锁：默认只读（防误触）。WebUI 为管理员页面，
        # 解锁对操作者开放；托管库即使解锁也保持系统只读。
        lock_key = f"kb_edit_unlocked_{kb}"
        if lock_key not in st.session_state:
            st.session_state[lock_key] = False
        unlocked = bool(st.session_state[lock_key])

        if not unlocked:
            if managed:
                st.caption(
                    "🔒 系统托管库（OA 同步 / 工单反哺）只读保护中：解锁后仅可修改可见范围，"
                    "不可上传、删除或重建索引。"
                )
            else:
                st.caption(
                    "🔒 知识库处于只读保护状态（防止误触改动），点击「解锁编辑」后可操作。"
                )
            if st.button(
                "🔓 解锁编辑",
                help=(
                    "解锁后仅可设置可见范围；刷新页面自动回到锁定。"
                    if managed
                    else "解锁后即可上传/删除/改简介/设置可见范围；刷新页面自动回到锁定。"
                ),
                key=f"kb_unlock_btn_{kb}",
            ):
                st.session_state[lock_key] = True
                st.rerun()
        else:
            st.caption(
                "🔓 已解锁编辑模式（本会话内有效）。"
                + ("（系统托管库：仅可修改可见范围）" if managed else "")
            )
            if st.button(
                "🔒 重新锁定",
                key=f"kb_relock_btn_{kb}",
                help="重新进入只读保护，防止后续误触。",
            ):
                st.session_state[lock_key] = False
                st.rerun()

        # readonly = 未解锁（会话级编辑锁）；content_editable = 未解锁且非托管
        readonly = not unlocked
        content_editable = unlocked and not managed
        st.session_state["selected_kb_info"] = kb_list[kb]["kb_info"]
        st.session_state["selected_kb_name"] = kb
        chunk_size, chunk_overlap, selected_rows = _render_files_section(
            api,
            kb,
            kb_list,
            readonly=readonly,
            managed=managed,
            content_editable=content_editable,
        )
        _render_kb_actions(
            api,
            kb,
            chunk_size,
            chunk_overlap,
            readonly=readonly,
            managed=managed,
            content_editable=content_editable,
        )
        _render_docs_editor(
            api, selected_kb, selected_rows, readonly=not content_editable
        )
