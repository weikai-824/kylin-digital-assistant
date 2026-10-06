"""检索后端故障的类型化异常。

与「无结果」严格区分：空结果意味着「确实没有匹配」，而本异常意味着
「检索后端（Milvus / ES）不可用或查询失败」——上层必须按故障处理，
不能伪装成无证据拒答。
"""

from __future__ import annotations


class RetrievalBackendError(RuntimeError):
    """检索后端故障（Milvus / Elasticsearch 查询失败或不可达）。"""

    def __init__(self, backend: str, kb_name: str = "", detail: str = ""):
        self.backend = backend
        self.kb_name = kb_name
        self.detail = detail
        msg = f"检索后端 {backend} 故障"
        if kb_name:
            msg += f"（知识库 {kb_name}）"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)
