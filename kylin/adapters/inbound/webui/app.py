"""系统咨询数字助手 — Streamlit 入口。"""

# 必须在 import 任何 streamlit 组件 / marshmallow 消费者之前完成兼容补丁。
from kylin.platform.runtime.compat_patches import apply_compat_patches

apply_compat_patches()

import streamlit as st
import streamlit_antd_components as sac

from kylin import __version__
from kylin.adapters.inbound.webui.kb_chat import kb_chat
from kylin.adapters.inbound.webui.knowledge_base import knowledge_base_page
from kylin.adapters.inbound.webui.tickets_page import tickets_page
from kylin.adapters.inbound.webui.utils import ApiRequest
from kylin.adapters.outbound.oa.config import load_oa_settings
from kylin.applications.identity.service import (
    IDENTITY_MODE_FIXED_DEV,
    fixed_dev_identity,
    identity_to_session_dict,
    resolve_identity_mode,
)
from kylin.platform.runtime.net import api_address

api = ApiRequest(base_url=api_address())

APP_NAME = "系统咨询数字助手"

# 钢蓝青绿 + 石板灰：办公向，避免紫渐变/奶油衬线套路
THEME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=Source+Serif+4:opsz,wght@8..60,600&display=swap');

html, body, [class*="css"] {
  font-family: "DM Sans", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}
.stApp {
  background: linear-gradient(165deg, #dfe8ee 0%, #eef3f6 42%, #e6edf2 100%);
}
[data-testid="stSidebar"] {
  background: #15202b !important;
  border-right: 1px solid rgba(255,255,255,0.06);
}
[data-testid="stSidebar"] * {
  color: #e8eef2 !important;
}
[data-testid="stSidebar"] .stSelectbox label,
[data-testid="stSidebar"] .stNumberInput label,
[data-testid="stSidebar"] .stSlider label,
[data-testid="stSidebar"] .stCheckbox label,
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] span {
  color: #c5d0da !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] > div,
[data-testid="stSidebar"] input,
[data-testid="stSidebar"] textarea {
  background-color: #1e2c3a !important;
  color: #e8eef2 !important;
  border-color: #314556 !important;
}
[data-testid="stSidebarUserContent"] {
  padding-top: 1rem;
  padding-bottom: 2rem;
}
.block-container {
  padding-top: 1.25rem;
  padding-bottom: 6rem;
  max-width: 1400px;
}
[data-testid="stBottomBlockContainer"] {
  padding-bottom: 12px;
  background: transparent;
}
#MainMenu, footer, header, .stDeployButton,
[data-testid="stToolbar"], [data-testid="stDecoration"] {
  visibility: hidden;
  display: none;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 4px 0 18px 0;
  padding: 4px 2px 14px;
  border-bottom: 1px solid rgba(255,255,255,0.08);
}
.brand-mark {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background: linear-gradient(145deg, #2f7f8a, #1f5c66);
  color: #f3fafb;
  font-family: "Source Serif 4", "Songti SC", serif;
  font-weight: 600;
  font-size: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  letter-spacing: 0.02em;
}
.brand-title {
  font-size: 15px;
  font-weight: 600;
  color: #f2f6f9 !important;
  line-height: 1.25;
  letter-spacing: 0.01em;
}
.brand-sub {
  font-size: 11px;
  color: #8fa0b0 !important;
  margin-top: 2px;
  line-height: 1.3;
}
.brand-ver {
  font-size: 11px;
  color: #6d7f90 !important;
  text-align: right;
  margin: 0 0 8px;
}

.main-header {
  margin-bottom: 0.75rem;
}
.main-header h1 {
  font-family: "Source Serif 4", "Songti SC", serif;
  font-size: 1.55rem !important;
  font-weight: 600 !important;
  color: #15202b !important;
  margin: 0 0 0.2rem 0 !important;
  letter-spacing: 0.01em;
}
.main-header .hint {
  color: #5a6b7a;
  font-size: 0.92rem;
  margin: 0;
}

.welcome {
  background: rgba(255,255,255,0.72);
  border: 1px solid rgba(21,32,43,0.08);
  border-radius: 14px;
  padding: 1.75rem 1.6rem;
  margin: 0.5rem 0 1rem;
}
.welcome h3 {
  margin: 0 0 0.4rem 0;
  color: #15202b;
  font-size: 1.15rem;
  font-weight: 600;
}
.welcome p {
  margin: 0 0 1rem 0;
  color: #5a6b7a;
  font-size: 0.95rem;
}
.welcome .chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.welcome .chip {
  display: inline-block;
  padding: 6px 12px;
  border-radius: 999px;
  background: #e7f1f3;
  color: #1f5c66;
  font-size: 0.85rem;
  border: 1px solid #c5dce0;
}

.evidence-panel {
  background: rgba(255,255,255,0.78);
  border: 1px solid rgba(21,32,43,0.08);
  border-radius: 14px;
  padding: 1rem 1rem 0.75rem;
  position: sticky;
  top: 0.75rem;
  max-height: calc(100vh - 7rem);
  overflow-y: auto;
}
.evidence-panel h3 {
  margin: 0 0 0.35rem 0;
  font-size: 0.95rem;
  color: #15202b;
  font-weight: 600;
}
.evidence-panel .sub {
  font-size: 0.8rem;
  color: #6d7f90;
  margin-bottom: 0.85rem;
}
.evidence-card {
  background: #f7fafb;
  border-left: 3px solid #2f7f8a;
  border-radius: 0 8px 8px 0;
  padding: 0.65rem 0.75rem;
  margin-bottom: 0.65rem;
}
.evidence-card .idx {
  font-size: 0.75rem;
  font-weight: 600;
  color: #2f7f8a;
  margin-bottom: 0.2rem;
}
.evidence-card .src {
  font-size: 0.82rem;
  color: #15202b;
  font-weight: 500;
  margin-bottom: 0.35rem;
  word-break: break-all;
}
.evidence-card .body {
  font-size: 0.8rem;
  color: #4a5b6a;
  line-height: 1.45;
  max-height: 7.5em;
  overflow: hidden;
}
.evidence-empty {
  color: #8a9aaa;
  font-size: 0.85rem;
  padding: 0.5rem 0 0.75rem;
}
.evidence-card .score {
  float: right;
  font-size: 0.7rem;
  color: #2f7f8a;
  background: rgba(47,127,138,0.12);
  border-radius: 8px;
  padding: 0.1rem 0.45rem;
  font-weight: 600;
}
.evidence-card details.ev-detail {
  margin-top: 0.25rem;
}
.evidence-card details.ev-detail summary {
  font-size: 0.75rem;
  color: #2f7f8a;
  cursor: pointer;
  user-select: none;
}
.evidence-card .ev-body {
  font-size: 0.78rem;
  color: #4a5b6a;
  line-height: 1.5;
  margin-top: 0.35rem;
  max-height: 16em;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  background: rgba(21,32,43,0.04);
  border-radius: 8px;
  padding: 0.5rem 0.6rem;
}
.ticket-banner {
  background: rgba(214,124,58,0.10);
  border: 1px solid rgba(214,124,58,0.35);
  border-radius: 12px;
  padding: 0.75rem 0.9rem;
  margin: 0.4rem 0 0.6rem;
}
.ticket-banner .tb-title {
  font-size: 0.86rem;
  font-weight: 600;
  color: #8a4a1c;
  margin-bottom: 0.3rem;
}
.ticket-banner .tb-body {
  font-size: 0.8rem;
  color: #6b5a44;
  line-height: 1.5;
}
.evidence-feedback {
  margin-top: 0.75rem;
  border-top: 1px dashed rgba(21,32,43,0.12);
  padding-top: 0.55rem;
}

div[data-testid="stChatMessage"] {
  background: rgba(255,255,255,0.55);
  border-radius: 12px;
  padding: 0.35rem 0.5rem;
  border: 1px solid rgba(21,32,43,0.05);
}

/* 流式问答：阶段指示器（理解问题 → 检索知识库 → 组织回答） */
.qa-stages {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin: 2px 0 6px;
}
.qa-stage {
  font-size: 12.5px;
  color: #93a1b0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  line-height: 1.4;
}
.qa-stage.done {
  color: #1f7a5c;
}
.qa-stage.active {
  color: #2f7f8a;
}
.qa-stage.active::before {
  content: "";
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #2f7f8a;
  animation: qa-pulse 1.1s ease-in-out infinite;
}
@keyframes qa-pulse {
  0%, 100% { opacity: 0.25; }
  50% { opacity: 1; }
}
.qa-stage-tip {
  font-size: 12px;
  color: #a3b0bc;
  margin-left: 2px;
}
.qa-error {
  color: #a32d2d;
}
</style>
"""


if __name__ == "__main__":
    st.set_page_config(
        page_title=APP_NAME,
        page_icon="💬",
        initial_sidebar_state="expanded",
        menu_items={"About": f"{APP_NAME} v{__version__}"},
        layout="wide",
    )

    st.markdown(THEME_CSS, unsafe_allow_html=True)

    with st.sidebar:
        st.markdown(
            f"""
            <div class="brand">
              <div class="brand-mark">咨</div>
              <div>
                <div class="brand-title">{APP_NAME}</div>
                <div class="brand-sub">企业制度 · 流程 · 规章问答</div>
              </div>
            </div>
            <p class="brand-ver">v{__version__}</p>
            """,
            unsafe_allow_html=True,
        )

        # ---- 身份（长期记忆按 userid 隔离）----
        # fixed_dev：本地默认无登录；oa_login：工号登录（上线再开）
        oa_settings = load_oa_settings()
        identity_mode = resolve_identity_mode(oa_settings)
        if identity_mode == IDENTITY_MODE_FIXED_DEV:
            fixed = identity_to_session_dict(fixed_dev_identity(oa_settings))
            prev = st.session_state.get("user") or {}
            if prev.get("userid") != fixed["userid"]:
                st.session_state["user"] = fixed
            st.markdown("#### 👤 身份")
            st.caption(f"本地开发 · {fixed['username']} · `{fixed['userid']}`")
        else:
            st.markdown("#### 👤 身份")
            user = st.session_state.get("user")
            if user:
                dept = user.get("dept") or ""
                st.caption(
                    f"工号 {user.get('job_num')} · {user.get('username')}"
                    + (f" · {dept}" if dept else "")
                    + ("（Mock）" if user.get("mock") else "")
                )
                if st.button("退出登录", use_container_width=True, key="logout_btn"):
                    st.session_state.pop("user", None)
                    st.rerun()
            else:
                with st.form("login_form", clear_on_submit=False):
                    job_num = st.text_input(
                        "工号", placeholder="输入 OA 工号", key="login_job_num"
                    )
                    submitted = st.form_submit_button("登录", use_container_width=True)
                if submitted:
                    job_num = (job_num or "").strip()
                    if not job_num:
                        st.warning("请输入工号")
                    else:
                        try:
                            resp = api.post(
                                "/knowledge_base/verify_user",
                                json={"job_num": job_num},
                            )
                            data = resp.json() if resp is not None else {}
                        except Exception as exc:  # noqa: BLE001
                            data = {
                                "code": 500,
                                "msg": f"身份校验服务不可用：{exc}",
                            }
                        if data.get("code") == 200:
                            st.session_state["user"] = data.get("data") or {}
                            st.rerun()
                        else:
                            st.error(data.get("msg") or "工号校验失败")
        st.divider()

        selected_page = sac.menu(
            [
                sac.MenuItem("对话", icon="chat-dots"),
                sac.MenuItem("工单", icon="clipboard-check"),
                sac.MenuItem("知识库", icon="folder2-open"),
            ],
            key="selected_page",
            open_index=0,
            color="#2f7f8a",
        )
        st.divider()

    if selected_page == "知识库":
        knowledge_base_page(api=api)
    elif selected_page == "工单":
        tickets_page(api=api)
    else:
        kb_chat(api=api)
