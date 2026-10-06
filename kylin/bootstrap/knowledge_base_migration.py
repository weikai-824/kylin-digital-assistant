"""知识库维护组合根：协调现有数据实现执行重建和清理。"""

import multiprocessing as mp
import time
from collections.abc import Sequence
from datetime import datetime
from typing import List, Literal

from kylin.adapters.outbound.mysql.database.base import Base, engine
from kylin.adapters.outbound.knowledge_base.kb_service.paths import list_kbs_from_folder
from kylin.adapters.outbound.model.clients import get_default_embedding
from kylin.bootstrap.knowledge_ingestion import build_knowledge_ingestion_service
from kylin.ports.application.knowledge_ingestion import KnowledgeIngestionPort
from kylin.ports.data.ingestion import FileOperationResult, KnowledgeFileDetails
from kylin.platform.config.settings import Settings
from kylin.platform.observability.logging import build_logger


logger = build_logger()


def worker(args: dict):
    """在独立进程中执行一组知识库维护操作。"""
    start_time = datetime.now()
    allow_system_managed = bool(args.get("system"))
    try:
        if args.get("create_tables"):
            create_tables()
        if args.get("clear_tables"):
            reset_tables()
            print("database tables reset")
        if args.get("recreate_vs"):
            create_tables()
            print("recreating all vector stores")
            folder2db(
                kb_names=args.get("kb_name"),
                mode="recreate_vs",
                embed_model=args.get("embed_model"),
                allow_system_managed=allow_system_managed,
            )
        elif args.get("update_in_db"):
            folder2db(
                kb_names=args.get("kb_name"),
                mode="update_in_db",
                embed_model=args.get("embed_model"),
                allow_system_managed=allow_system_managed,
            )
        elif args.get("increment"):
            folder2db(
                kb_names=args.get("kb_name"),
                mode="increment",
                embed_model=args.get("embed_model"),
                allow_system_managed=allow_system_managed,
            )
        elif args.get("prune_db"):
            prune_db_docs(args.get("kb_name"), allow_system_managed=allow_system_managed)
        elif args.get("prune_folder"):
            prune_folder_files(args.get("kb_name"))
        print(f"总计用时\t：{datetime.now() - start_time}\n")
    except Exception as error:
        logger.exception(error)
        raise


def run_worker(args: dict) -> None:
    """启动维护子进程并保持旧命令的中断语义。"""
    process = mp.Process(target=worker, args=(args,), daemon=True)
    process.start()
    try:
        while process.is_alive():
            time.sleep(0.1)
    except KeyboardInterrupt:
        logger.warning("Caught KeyboardInterrupt! Setting stop event...")
        process.terminate()
        process.join()
        raise SystemExit() from None

    process.join()
    if process.exitcode:
        raise RuntimeError(f"Knowledge base maintenance failed with status {process.exitcode}")


def create_tables():
    import kylin.adapters.outbound.mysql.database.models  # noqa: F401 — 注册全部 ORM 表

    Base.metadata.create_all(bind=engine)


def reset_tables():
    import kylin.adapters.outbound.mysql.database.models  # noqa: F401

    Base.metadata.drop_all(bind=engine)
    create_tables()


def _file_details(
    service: KnowledgeIngestionPort,
    kb_name: str,
) -> tuple[KnowledgeFileDetails, ...]:
    try:
        return tuple(service.list_file_details(kb_name))
    except LookupError:
        return ()


def _print_maintenance_summary(
    service: KnowledgeIngestionPort,
    kb_name: str,
    embed_model: str,
    filenames: Sequence[str],
    results: Sequence[FileOperationResult],
    start: datetime,
) -> None:
    details = _file_details(service, kb_name)
    successful_filenames = {
        result.filename for result in results if result.success
    }
    docs_count = sum(
        item.docs_count
        for item in details
        if item.filename in successful_filenames and item.in_db
    )
    success_count = sum(result.success for result in results)
    print("\n" + "-" * 100)
    print(
        (
            f"知识库名称\t：{kb_name}\n"
            f"知识库类型\t：dual\n"
            f"向量模型：\t：{embed_model}\n"
            f"文件总数量\t：{len(filenames)}\n"
            f"入库文件数\t：{success_count}\n"
            f"知识条目数\t：{docs_count}\n"
            f"用时\t\t：{datetime.now()-start}"
        )
    )
    print("-" * 100 + "\n")


def folder2db(
    kb_names: List[str],
    mode: Literal["recreate_vs", "update_in_db", "increment"],
    vs_type: Literal["dual"] = None,
    embed_model: str = None,
    chunk_size: int = None,
    chunk_overlap: int = None,
    allow_system_managed: bool = False,
):
    """
    use existed files in local folder to populate database and/or vector store.
    set parameter `mode` to:
        recreate_vs: recreate all vector store and fill info to database using existed files in local folder
        fill_info_only(disabled): do not create vector store, fill info to db using existed files only
        update_in_db: update vector store and database info using local files that existed in database only
        increment: create vector store and database info for local files that not existed in database only

    allow_system_managed=True（CLI --system）：允许对 OA/工单托管库执行重建等维护操作。
    """

    # 默认值延迟到调用时解析：避免 import 即读配置/发网络请求
    vs_type = vs_type or Settings.kb_settings.DEFAULT_VS_TYPE
    embed_model = embed_model or get_default_embedding()
    chunk_size = chunk_size if chunk_size is not None else Settings.kb_settings.CHILD_CHUNK_SIZE
    chunk_overlap = (
        chunk_overlap if chunk_overlap is not None else Settings.kb_settings.CHILD_OVERLAP_SIZE
    )

    kb_names = kb_names or list_kbs_from_folder()
    service = build_knowledge_ingestion_service()
    for kb_name in kb_names:
        start = datetime.now()
        filenames: List[str] = []
        results: Sequence[FileOperationResult] = ()
        if mode == "recreate_vs":
            results = service.rebuild(
                kb_name,
                allow_empty_kb=True,
                vector_store_type=vs_type,
                embedding_model=embed_model,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                allow_system_managed=allow_system_managed,
            )
            filenames = [result.filename for result in results]
        elif mode == "update_in_db":
            filenames = [
                item.filename
                for item in _file_details(service, kb_name)
                if item.in_db
            ]
            if filenames:
                results = service.update_files(
                    kb_name,
                    filenames,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    allow_system_managed=allow_system_managed,
                )
        elif mode == "increment":
            filenames = [
                item.filename
                for item in _file_details(service, kb_name)
                if item.in_folder and not item.in_db
            ]
            if filenames:
                results = service.update_files(
                    kb_name,
                    filenames,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    allow_system_managed=allow_system_managed,
                )
        else:
            print(f"unsupported migrate mode: {mode}")
        _print_maintenance_summary(
            service,
            kb_name,
            embed_model,
            filenames,
            results,
            start,
        )


def prune_db_docs(kb_names: List[str], *, allow_system_managed: bool = False):
    """
    delete docs in database that not existed in local folder.
    it is used to delete database docs after user deleted some doc files in file browser
    """
    service = build_knowledge_ingestion_service()
    for kb_name in kb_names:
        files = [
            item.filename
            for item in _file_details(service, kb_name)
            if item.in_db and not item.in_folder
        ]
        results = (
            service.delete_files(
                kb_name,
                files,
                delete_content=False,
                allow_system_managed=allow_system_managed,
            )
            if files
            else ()
        )
        for result in results:
            if result.success:
                print(f"success to delete docs for file: {kb_name}/{result.filename}")


def prune_folder_files(kb_names: List[str]):
    """
    delete doc files in local folder that not existed in database.
    it is used to free local disk space by delete unused doc files.
    """
    service = build_knowledge_ingestion_service()
    for kb_name in kb_names:
        files = [
            item.filename
            for item in _file_details(service, kb_name)
            if item.in_folder and not item.in_db
        ]
        results = (
            service.delete_files(kb_name, files, delete_content=True)
            if files
            else ()
        )
        for result in results:
            if result.success:
                print(f"success to delete file: {kb_name}/{result.filename}")
