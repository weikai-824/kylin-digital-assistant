"""切分后元数据：为后续检索 / 过滤 / 引用打标。

不做入库、不碰 OA。字段可后续按需追加。
"""

from .enrich import enrich_chunks
from .schema import METADATA_SCHEMA_VERSION

__all__ = [
    "METADATA_SCHEMA_VERSION",
    "enrich_chunks",
]
