"""共享文本模式：表格行 / 分隔行等，多阶段（clean / split / metadata）统一引用。

此前 _TABLE_ROW / _TABLE_SEP 在 parent_child.py、markdown_clean.py、enrich.py
各复制一份，已收敛为本模块唯一来源。
"""

from __future__ import annotations

import re

# 表格数据行（判断用，宽松：允许空单元格 `| |`）
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
# 表格数据行（提取用：捕获单元格内容，供 cells 拆解）
TABLE_ROW_CELLS = re.compile(r"^\s*\|(.+)\|\s*$")
# 表格分隔行 |---|---|
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
