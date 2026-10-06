"""父子块切分：结构切 Parent，递归切 Child。

只负责把清洗后的 Markdown 切成带父子关系的块。
入库/检索侧应对齐 LangChain ParentDocumentRetriever 约定：
子块进向量库（metadata.doc_id → 父 id），父块进独立 docstore。
"""

from __future__ import annotations

import re
import uuid

from kylin.adapters.outbound.knowledge_base.clean.patterns import TABLE_ROW, TABLE_SEP
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

# 标题行：# ~ ######
# 注意 [ \t] 而非 \s：\s 会吞掉换行，导致"空标题行 ##  + 下一行"被合并匹配，
# 例如 docling 输出的 "## \n\n### 一、..." 会被误认为一个标题，group(2) 带上 ### 前缀。
_HEADER = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def _clean_title(raw: str) -> str:
    """剥离标题里的 markdown 语法残留（# 前缀、首尾空白），返回干净标题。"""
    t = (raw or "").strip()
    t = re.sub(r"^#+\s*", "", t)
    return t.strip()
# 面包屑分隔符：不用 "/"（OA 标题常含斜杠，会切错层级）
TITLE_SEP = " > "

# 中文递归分隔符（优先级从粗到细）
_SEPARATORS: Tuple[str, ...] = (
    "\n\n",
    "\n",
    r"。|！|？",
    r"\.\s|\!\s|\?\s",
    r"；|;\s",
    r"，|,\s",
)


@dataclass
class ChildChunk:
    """检索粒度：小块。"""

    id: str
    parent_id: str
    text: str
    index: int
    title_path: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParentChunk:
    """生成粒度：章节 / 大段。"""

    id: str
    text: str
    index: int
    title: str = ""
    title_path: str = ""
    level: int = 0
    children: List[ChildChunk] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


def split_markdown_parent_child(
    text: str,
    *,
    parent_chunk_size: int = 1200,
    child_chunk_size: int = 250,
    child_overlap: int = 40,
    parent_header_levels: Sequence[int] = (1, 2),
) -> List[ParentChunk]:
    """清洗后的 Markdown → 父子块列表。

    Parent：按标题（默认 h1–h2）切节，更深标题留在节内；超长再递归切，尽量不拆表。
    Child：在每个 Parent 内递归切到 child_chunk_size；整表不跨 Child，超长表按行组切。

    child_overlap：相邻 Child（及超长 Parent 切片）的目标重叠字符数。

    - ``>= 0``（生产目标值，配置项 ``KB.CHILD_OVERLAP_SIZE``）：走 LangChain 语义，
      缓冲尾保留到约该长度，起到真滑动窗口的作用；
    - ``< 0``：**基线哨兵**。历史实现里本参数被静默忽略、且缓冲尾被最大化保留
      （详见 ``_merge_splits`` 文档），传负数可逐字节复现旧行为，仅用于改动前后对照，
      生产不应使用。

    ``child_overlap`` 同时作用于父块（超长节递归切）与子块，二者共用同一实现。
    """
    text = (text or "").strip()
    if not text:
        return []

    sections = _split_by_headers(text, set(parent_header_levels))
    sections = _merge_orphan_title_sections(sections)
    parents: List[ParentChunk] = []
    parent_index = 0

    for title, level, title_path, body in sections:
        body = body.strip()
        if not body and not title:
            continue
        # Parent 正文含标题行，便于后续展开时自带章节名
        if title and level > 0:
            section_text = f"{'#' * level} {title}\n\n{body}".strip()
        else:
            section_text = body
        if not section_text:
            continue

        for piece in _split_oversized_parent(
            section_text, parent_chunk_size, child_overlap
        ):
            pid = uuid.uuid4().hex[:12]
            # 超长切出的后续片沿用同一 title_path
            parents.append(
                ParentChunk(
                    id=pid,
                    text=piece,
                    index=parent_index,
                    title=title,
                    title_path=title_path,
                    level=level,
                )
            )
            parent_index += 1

    for parent in parents:
        child_parts = _split_to_children(
            parent.text,
            child_chunk_size,
            base_title_path=parent.title_path,
            header_cut_level=max(parent_header_levels) + 1,
            overlap=child_overlap,
        )
        parent.children = [
            ChildChunk(
                id=f"{parent.id}_{i}",
                parent_id=parent.id,
                text=ct,
                index=i,
                title_path=cp,
            )
            for i, (ct, cp) in enumerate(child_parts)
        ]

    return parents


def _split_by_headers(
    text: str, levels: set
) -> List[Tuple[str, int, str, str]]:
    """按指定标题层级切开，返回 (title, level, title_path, body)。"""
    matches = list(_HEADER.finditer(text))
    if not matches:
        return [("", 0, "", text)]

    # 只在目标层级处切开；更深标题留在 body 里
    cut_points = [m for m in matches if len(m.group(1)) in levels]
    if not cut_points:
        return [("", 0, "", text)]

    stack: List[Tuple[int, str]] = []  # (level, title) 用于面包屑
    sections: List[Tuple[str, int, str, str]] = []

    # 首个切点之前的序言
    first = cut_points[0]
    preamble = text[: first.start()].strip()
    if preamble:
        sections.append(("", 0, "", preamble))

    for i, m in enumerate(cut_points):
        level = len(m.group(1))
        title = _clean_title(m.group(2))
        start = m.end()
        end = cut_points[i + 1].start() if i + 1 < len(cut_points) else len(text)
        body = text[start:end].strip()

        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        title_path = TITLE_SEP.join(t for _, t in stack)

        sections.append((title, level, title_path, body))

    return sections


def _merge_orphan_title_sections(
    sections: List[Tuple[str, int, str, str]],
) -> List[Tuple[str, int, str, str]]:
    """孤儿标题节（有标题但正文为空）并入下一节，避免产出"光标题"父块。

    背景：解析层对双栏/表格版式的归属错位，会把某标题下的内容整体归到
    相邻节，产生只剩标题行的 Parent（实例：OA 手册第 15 页"单点登录"
    清单被整体归进"统一待办"节，"单点登录"节只剩 22 字标题行，检索
    命中后 LLM 拿不到正文只能答"未列出具体系统名称"）。

    合并策略：把孤儿标题行按原文顺序塞进下一个有正文节的开头；该节的
    标题/面包屑/层级不变（内容归属不动，只是让标题文本与正文同块可见）。
    连续孤儿标题级联合并；文档末尾孤儿无处可并则原样保留。

    注意：这是切分层的兜底，治标——根因在解析层归属错位，换解析策略后
    本函数自然退化成空转（没有孤儿节可合并）。
    """
    out: List[Tuple[str, int, str, str]] = []
    pending: List[Tuple[str, int, str, str]] = []  # 待合并的孤儿节（原样暂存）
    for section in sections:
        title, level, title_path, body = section
        if title and not body.strip():
            pending.append(section)
            continue
        if pending:
            header_lines = "\n\n".join(
                f"{'#' * lv} {t}" for t, lv, _tp, _b in pending
            )
            body = f"{header_lines}\n\n{body}".strip() if body else header_lines
            pending = []
        out.append((title, level, title_path, body))
    # 末尾剩余孤儿（无下一节可并）：保持原样
    out.extend(pending)
    return out


def _split_oversized_parent(text: str, max_size: int, overlap: int = -1) -> List[str]:
    """Parent 超长时按块切开，整表尽量落在同一片；超长表按行组切。"""
    if len(text) <= max_size:
        return [text]

    blocks = _segment_preserving_tables(text)
    pieces: List[str] = []
    buf: List[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            pieces.append("\n\n".join(buf).strip())
            buf = []
            buf_len = 0

    for block in blocks:
        blen = len(block)
        if blen > max_size and _is_table_block(block):
            flush()
            pieces.extend(_split_table(block, max_size))
            continue
        if blen > max_size:
            flush()
            pieces.extend(_recursive_split(block, max_size, overlap))
            continue
        # 整表或普通块：能塞进当前片就合并，否则新开一片
        if buf and buf_len + 2 + blen > max_size:
            flush()
        buf.append(block)
        buf_len += blen + (2 if buf_len else 0)

    flush()
    return [p for p in pieces if p]


def _split_to_children(
    text: str,
    chunk_size: int,
    *,
    base_title_path: str = "",
    header_cut_level: int = 3,
    overlap: int = -1,
) -> List[Tuple[str, str]]:
    """Parent 内切 Child，返回 [(text, title_path)]。

    - 表块整段保留；超长表按"表头 + 每 N 行"切组
    - header_cut_level 及以上深度的标题成为 Child 语义边界，
      并把该层级标题追加进 title_path（解决 h3 语义丢失）
    """
    if len(text) <= chunk_size and not _has_deeper_header(text, header_cut_level):
        return [(text, base_title_path)]

    blocks = _segment_preserving_tables(text)
    children: List[Tuple[str, str]] = []
    buf: List[str] = []
    buf_len = 0
    title_stack: List[Tuple[int, str]] = []  # (level, title)

    def sub_path() -> str:
        sub = TITLE_SEP.join(t for _, t in title_stack)
        if not sub:
            return base_title_path
        if base_title_path:
            return f"{base_title_path}{TITLE_SEP}{sub}"
        return sub

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            children.append(("\n\n".join(buf).strip(), sub_path()))
            buf = []
            buf_len = 0

    for block in blocks:
        m = _HEADER.match(block.strip())
        if m and len(m.group(1)) >= header_cut_level:
            # 更深标题：新小节边界，标题行并入下一组
            flush()
            level = len(m.group(1))
            while title_stack and title_stack[-1][0] >= level:
                title_stack.pop()
            title_stack.append((level, _clean_title(m.group(2))))
            buf.append(block)
            buf_len += len(block)
            continue
        if _is_table_block(block):
            flush()
            for piece in _split_table(block, chunk_size):
                children.append((piece.strip(), sub_path()))
            continue
        if len(block) <= chunk_size:
            if buf and buf_len + 2 + len(block) > chunk_size:
                flush()
            buf.append(block)
            buf_len += len(block) + (2 if buf_len else 0)
        else:
            flush()
            for piece in _recursive_split(block, chunk_size, overlap):
                children.append((piece, sub_path()))

    flush()
    return [c for c in children if c[0]]


def _has_deeper_header(text: str, header_cut_level: int) -> bool:
    """文本内是否存在 header_cut_level 及更深级别的标题行。"""
    if header_cut_level > 6:
        return False
    for m in _HEADER.finditer(text):
        if len(m.group(1)) >= header_cut_level:
            return True
    return False


def _split_table(text: str, max_size: int) -> List[str]:
    """超长表按行切组：表头 + 分隔行保留，数据行按行累积到接近 max_size。

    单行本身超长（单元格巨长）时整行单独成块，不再继续拆分。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 2:
        return [text]

    # 定位分隔行（第二行通常为 |---|---| 形式；找不到则退化按行均分）
    sep_idx = None
    for i, ln in enumerate(lines[1:], start=1):
        if TABLE_SEP.match(ln):
            sep_idx = i
            break
    if sep_idx is None:
        header_lines: List[str] = []
        data_lines = lines
    else:
        header_lines = lines[: sep_idx + 1]
        data_lines = lines[sep_idx + 1 :]

    header_text = "\n".join(header_lines)
    if not data_lines:
        return [header_text]

    groups: List[List[str]] = []
    cur: List[str] = []
    cur_len = len(header_text)
    for ln in data_lines:
        add = len(ln) + 1
        if cur and cur_len + add > max_size:
            groups.append(cur)
            cur = []
            cur_len = len(header_text)
        cur.append(ln)
        cur_len += add
    if cur:
        groups.append(cur)

    return ["\n".join(header_lines + g).strip() for g in groups]


def _segment_preserving_tables(text: str) -> List[str]:
    """按空行分段，连续表行合成一块。"""
    lines = text.splitlines()
    blocks: List[str] = []
    buf: List[str] = []
    in_table = False

    def flush_buf() -> None:
        nonlocal buf
        if buf:
            blocks.append("\n".join(buf).strip())
            buf = []

    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_table_line(line):
            if not in_table:
                flush_buf()
                in_table = True
            buf.append(line)
            i += 1
            continue
        if in_table:
            # 表后空行吃掉，结束表块
            flush_buf()
            in_table = False
            if not line.strip():
                i += 1
                continue
        if not line.strip():
            flush_buf()
            i += 1
            continue
        buf.append(line)
        i += 1

    flush_buf()
    return [b for b in blocks if b]


def _is_table_line(line: str) -> bool:
    return bool(TABLE_ROW.match(line) or TABLE_SEP.match(line))


def _is_table_block(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    return all(_is_table_line(ln) for ln in lines)


def _recursive_split(text: str, chunk_size: int, overlap: int = -1) -> List[str]:
    """按中文分隔符递归切分（逻辑源自上游 ChineseRecursiveTextSplitter）。"""
    return _split_text(text, list(_SEPARATORS), chunk_size, overlap)


def _split_text_with_regex(text: str, separator: str, keep_separator: bool) -> List[str]:
    if not separator:
        return list(text)
    if keep_separator:
        _splits = re.split(f"({separator})", text)
        splits = ["".join(i) for i in zip(_splits[0::2], _splits[1::2])]
        if len(_splits) % 2 == 1:
            splits += _splits[-1:]
    else:
        splits = re.split(separator, text)
    return [s for s in splits if s != ""]


def _merge_splits(
    splits: List[str],
    separator: str,
    chunk_size: int,
    overlap: int = -1,
) -> List[str]:
    """按 chunk_size 合并碎片，并按 overlap 控制相邻块的重叠量。

    历史坑（2026-09-10 定位）：本函数是上游 LangChain ``_merge_splits`` 的改写版，
    但判据里的 ``total > self._chunk_overlap`` 被写成了 ``total > 0`` —— 于是"丢缓冲头"
    只在"下一段塞不下"时才发生，缓冲尾被**最大限度地**保留，等价于 overlap ≈ chunk_size
    而非配置值 0/40。实测 docling 12 份输出的 chunk 文本量是解析输出的 5.9 倍，
    2455 个 child 里绝大多数是滑窗冗余（唯一块口径 1733，真实有效块约 640）。

    参数 ``overlap``：
    - ``>= 0``：LangChain 语义，缓冲尾保留到约 overlap 个字符（生产目标行为）；
    - ``< 0`` ：旧行为（最大重叠），仅用于改动前后的基线对照，验证完成后应弃用。
    """
    sep_len = len(separator)
    langchain_mode = overlap >= 0
    docs: List[str] = []
    current: List[str] = []
    total = 0
    for d in splits:
        _len = len(d)
        if total + _len + (sep_len if current else 0) > chunk_size:
            if current:
                doc = separator.join(current)
                if doc.strip():
                    docs.append(doc)
                while current:
                    if langchain_mode:
                        # 缓冲尾保留到约 overlap 字符（LangChain 原判据）
                        if not (
                            total > overlap
                            or (
                                total + _len + (sep_len if current else 0) > chunk_size
                                and total > 0
                            )
                        ):
                            break
                    else:
                        # 旧行为：只丢到"下一段能塞下"为止 → 保留最大可能的尾部
                        if not (
                            total > 0
                            and (
                                total > chunk_size
                                or total + _len + (sep_len if current else 0) > chunk_size
                            )
                        ):
                            break
                    total -= len(current[0]) + (sep_len if len(current) > 1 else 0)
                    current = current[1:]
        current.append(d)
        total += _len + (sep_len if len(current) > 1 else 0)
    if current:
        doc = separator.join(current)
        if doc.strip():
            docs.append(doc)
    return docs


def _split_text(
    text: str, separators: List[str], chunk_size: int, overlap: int = -1
) -> List[str]:
    final_chunks: List[str] = []
    separator = separators[-1]
    new_separators: List[str] = []
    for i, s in enumerate(separators):
        if s == "":
            separator = s
            break
        if re.search(s, text):
            separator = s
            new_separators = separators[i + 1 :]
            break

    splits = _split_text_with_regex(text, separator, keep_separator=True)
    good: List[str] = []
    merge_sep = ""
    for s in splits:
        if len(s) < chunk_size:
            good.append(s)
        else:
            if good:
                final_chunks.extend(_merge_splits(good, merge_sep, chunk_size, overlap))
                good = []
            if not new_separators:
                final_chunks.append(s)
            else:
                final_chunks.extend(_split_text(s, new_separators, chunk_size, overlap))
    if good:
        final_chunks.extend(_merge_splits(good, merge_sep, chunk_size, overlap))
    return [
        re.sub(r"\n{2,}", "\n", chunk.strip())
        for chunk in final_chunks
        if chunk.strip()
    ]
