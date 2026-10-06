"""access_depts 迁移脚本：给存量 file_doc 台账补上 access_depts。

背景：v2 权限模型引入 access_depts（允许访问部门列表），存量 OA 文档只有
department_id（归属部门）。本脚本把"有 department_id 但无 access_depts"的
子块记录补上 access_depts=[department_id]（保持 OA 文档仅本部门可见的语义）。

仅迁移 MySQL file_doc 台账；Milvus/ES 索引的 access_depts 在统一重建知识库时
落地（OA 重同步打标 / Web/工单重建从台账读回）。

用法（conda py312）：
    python -m kylin.adapters.inbound.jobs.migrate_access_depts [--dry-run]
"""

from __future__ import annotations

import argparse

from kylin.platform.config.settings import Settings
from kylin.adapters.outbound.mysql.database.session import with_session


@with_session
def _migrate(session, *, dry_run: bool) -> tuple[int, int]:
    """为有 department_id 但缺 access_depts 的 file_doc 记录补 access_depts。

    返回 (应迁移条数, 实际迁移条数)。
    """
    from kylin.adapters.outbound.mysql.database.models.knowledge_file_model import FileDocModel

    rows = session.query(FileDocModel).all()
    pending = 0
    migrated = 0
    for row in rows:
        meta = row.meta_data or {}
        dept = str(meta.get("department_id") or "").strip()
        has_access = bool(meta.get("access_depts"))
        if dept and not has_access:
            pending += 1
            if not dry_run:
                new_meta = dict(meta)
                new_meta["access_depts"] = [dept]
                row.meta_data = new_meta
                migrated += 1
    if not dry_run:
        session.commit()
    return pending, migrated


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移 file_doc 台账：department_id → access_depts")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计不写入",
    )
    args = parser.parse_args()

    print(f"台账库: {Settings.basic_settings.SQLALCHEMY_DATABASE_URI}")
    pending, migrated = _migrate(dry_run=args.dry_run)
    if args.dry_run:
        print(f"[dry-run] 将有 {pending} 条 file_doc 记录补 access_depts=[department_id]")
    else:
        print(f"已迁移 {migrated}/{pending} 条记录补 access_depts")


if __name__ == "__main__":
    main()
