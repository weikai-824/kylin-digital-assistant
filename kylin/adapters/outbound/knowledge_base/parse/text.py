"""无第三方依赖的 Markdown 文本加载器。"""

from __future__ import annotations

from pathlib import Path

from kylin.platform.runtime.documents import Document


class TextDocumentLoader:
    """读取 UTF-8 文本文件并转换为项目内部 Document。"""

    def __init__(self, file_path: str, **_kwargs):
        self.file_path = file_path

    def load(self) -> list[Document]:
        text = Path(self.file_path).read_text(encoding="utf-8")
        return [
            Document(
                page_content=text,
                metadata={"source": Path(self.file_path).name},
            )
        ]
