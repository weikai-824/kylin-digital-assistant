"""知识库内容目录和文件路径工具。"""

from __future__ import annotations

import os
from pathlib import Path

from kylin.platform.config.settings import Settings


def validate_kb_name(knowledge_base_id: str) -> bool:
    """知识库名：防路径穿越；建议仅用 ASCII（Milvus/ES 索引名会再做 safe 化）。

    拒绝任何含路径分隔符或为 ``.`` / ``..`` 的名字——``os.path.join`` 对
    ``..`` 会解析到父目录，对 Windows 绝对路径（``C:\\x``）会直接覆盖前缀，
    两者都会让知识库落到 KB_ROOT 之外。
    """
    name = (knowledge_base_id or "").strip()
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return True


def get_kb_path(knowledge_base_name: str):
    """知识库根目录；越界名字直接抛错，不静默落到 KB_ROOT 之外。"""
    root = Path(Settings.basic_settings.KB_ROOT_PATH).resolve()
    kb_path = (root / knowledge_base_name).resolve()
    if not kb_path.is_relative_to(root):
        raise ValueError(f"非法的知识库名: {knowledge_base_name!r}")
    return str(kb_path)


def get_doc_path(knowledge_base_name: str):
    return os.path.join(get_kb_path(knowledge_base_name), "content")


def get_vs_path(knowledge_base_name: str, vector_name: str):
    return os.path.join(get_kb_path(knowledge_base_name), "vector_store", vector_name)


def get_file_path(knowledge_base_name: str, doc_name: str):
    """知识库内文件的绝对路径；越界一律抛错。

    用 ``is_relative_to`` 而非 ``str.startswith``：后者无路径边界，
    ``content/`` 会被 ``content_evil/`` 这类同前缀兄弟目录蒙混过关。
    """
    doc_path = Path(get_doc_path(knowledge_base_name)).resolve()
    file_path = (doc_path / doc_name).resolve()
    if not file_path.is_relative_to(doc_path):
        raise ValueError(f"非法的文件名: {doc_name!r}")
    return str(file_path)


def list_kbs_from_folder():
    return [
        f
        for f in os.listdir(Settings.basic_settings.KB_ROOT_PATH)
        if os.path.isdir(os.path.join(Settings.basic_settings.KB_ROOT_PATH, f))
    ]


def list_files_from_folder(kb_name: str):
    doc_path = get_doc_path(kb_name)
    result = []

    def is_skiped_path(path: str):
        tail = os.path.basename(path).lower()
        for x in ["temp", "tmp", ".", "~$"]:
            if tail.startswith(x):
                return True
        return False

    def process_entry(entry):
        if is_skiped_path(entry.path):
            return

        if entry.is_symlink():
            target_path = os.path.realpath(entry.path)
            with os.scandir(target_path) as target_it:
                for target_entry in target_it:
                    process_entry(target_entry)
        elif entry.is_file():
            file_path = Path(
                os.path.relpath(entry.path, doc_path)
            ).as_posix()  # 路径统一为 posix 格式
            result.append(file_path)
        elif entry.is_dir():
            with os.scandir(entry.path) as it:
                for sub_entry in it:
                    process_entry(sub_entry)

    with os.scandir(doc_path) as it:
        for entry in it:
            process_entry(entry)

    return result
