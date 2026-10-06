"""第三方库版本属性兼容补丁。

marshmallow 4 移除了 ``__version__`` / ``__version_info__``；environs<=9.5
（pymilvus 2.4.9 传递依赖）仍会在 import 时读取它们。
streamlit 1.34 移除了 ``streamlit.runtime.scriptrunner.version_info``，
部分老 Streamlit 组件仍会访问。

必须在导入 pymilvus / environs / streamlit 组件之前调用。
"""

from __future__ import annotations

_applied = False


def apply_compat_patches() -> None:
    """幂等地补齐 marshmallow 与 streamlit scriptrunner 的版本属性。"""
    global _applied
    if _applied:
        return
    _patch_marshmallow()
    _patch_streamlit_scriptrunner()
    _applied = True


def _patch_marshmallow() -> None:
    try:
        import marshmallow as mm
    except ImportError:
        return

    version = getattr(mm, "__version__", None)
    if not version:
        try:
            from importlib.metadata import version as pkg_version

            version = pkg_version("marshmallow")
        except Exception:
            version = "3.26.1"
        mm.__version__ = version

    info = getattr(mm, "__version_info__", None)
    if info is None:
        parts = []
        for part in str(version).split(".")[:3]:
            try:
                parts.append(int(part))
            except ValueError:
                break
        while len(parts) < 3:
            parts.append(0)
        info = tuple(parts)
        mm.__version_info__ = info

    # 少数误用名访问（非官方属性）
    if not hasattr(mm, "version_info"):
        mm.version_info = info


def _patch_streamlit_scriptrunner() -> None:
    try:
        import streamlit.runtime.scriptrunner as scriptrunner
    except ImportError:
        return
    if not hasattr(scriptrunner, "version_info"):
        scriptrunner.version_info = (1, 34, 0)
