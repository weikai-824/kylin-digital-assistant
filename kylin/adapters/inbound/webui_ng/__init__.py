"""NiceGUI 版 WebUI 入站适配器（:8501 后台调试台）。

8501 收敛后的页面：
- /       知识缺口工单处理（取代原对话调试页）
- /kb     知识库管理 · 文件上传

原旧版 Streamlit webui 目录保留作参考（KYLIN_WEBUI_LEGACY=1 才回退）。
"""

from kylin.adapters.inbound.webui_ng.main import create_webui_app

__all__ = ["create_webui_app"]
