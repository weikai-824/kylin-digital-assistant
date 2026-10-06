"""批量「只刷权限」：改写存量文档子块的 access_depts，不重新切分/不重新 embed。

背景：OA 等文档的 access_depts 是同步/入库时固化的快照。改全局开关
（oa_settings.document_public_by_default）只影响后续同步，存量文档身上
的 access_depts=[部门] 仍会按旧权限过滤。本脚本直接改写这些字段：
- 只更新双索引（Milvus 读回原向量写回 + ES）与 MySQL 台账，不重灌内容；
- 检索过滤立即生效，重建知识库时权限也不会被台账旧值回读覆盖。

用法（conda kylin311）：
    python -m kylin.adapters.inbound.jobs.refresh_access_depts --kb oa_kb --public
    python -m kylin.adapters.inbound.jobs.refresh_access_depts --kb oa_kb --depts "D1,D2"
    python -m kylin.adapters.inbound.jobs.refresh_access_depts --kb oa_kb --file "x.pdf" --public --dry-run
"""

from __future__ import annotations

import argparse
import sys

from kylin.bootstrap.knowledge_ingestion import build_knowledge_ingestion_service


def main() -> None:
    parser = argparse.ArgumentParser(
        description="批量改写存量文档子块 access_depts（只刷权限，不重新索引内容）"
    )
    parser.add_argument(
        "--kb",
        action="append",
        default=[],
        help="知识库名，可多次指定；缺省则处理所有知识库",
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        help="文件相对路径，可多次指定；缺省则处理该库所有已建索引文件",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--public", action="store_true", help="置为全开放（access_depts 置空）")
    mode.add_argument("--depts", help="改为仅指定部门可见，如 \"D1,D2\"")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计不写入",
    )
    args = parser.parse_args()

    target: list[str] = []
    if args.depts:
        target = [
            d.strip()
            for d in args.depts.replace("，", ",").replace(";", ",").split(",")
            if d.strip()
        ]

    svc = build_knowledge_ingestion_service()
    kb_names = args.kb or [kb.name for kb in svc.list_knowledge_bases()]

    scanned = updated = skipped = 0
    for kb_name in kb_names:
        details = [d for d in svc.list_file_details(kb_name)]
        if args.file:
            wanted = set(args.file)
            details = [d for d in details if d.filename in wanted]
        for d in details:
            if not d.in_db:
                skipped += 1
                continue
            scanned += 1
            # 匿名/缺部门文档默认全开放，避免误把公共文档收窄到某部门。
            if target and not d.department_id:
                skipped += 1
                continue
            if args.dry_run:
                print(f"[dry-run] {kb_name}/{d.filename}: 现有 {d.access_depts or '全开放'} -> {target or '全开放'}")
                updated += 1
                continue
            try:
                n = svc.refresh_doc_access_depts(kb_name, d.filename, target)
                if n == 0:
                    print(f"[skip] {kb_name}/{d.filename}: 索引无子块，跳过")
                    skipped += 1
                else:
                    print(f"[ok]   {kb_name}/{d.filename}: 改写 {n} 个子块 access_depts={target or '全开放'}")
                    updated += 1
            except Exception as e:
                print(f"[fail] {kb_name}/{d.filename}: {e}", file=sys.stderr)
    print(
        f"\n完成：扫描 {scanned}，更新 {updated}，跳过 {skipped}"
        + ("（dry-run，未写入）" if args.dry_run else "")
    )


if __name__ == "__main__":
    main()