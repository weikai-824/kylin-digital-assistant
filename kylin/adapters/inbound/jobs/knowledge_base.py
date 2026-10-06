"""知识库维护任务的 Click 入站适配器。"""

from __future__ import annotations

from collections.abc import Callable

import click


def create_knowledge_base_command(
    run_worker: Callable[[dict], None],
) -> click.Command:
    """创建由组合根注入维护执行器的知识库命令。"""

    @click.command(help="知识库相关功能")
    @click.option(
        "-r",
        "--recreate-vs",
        is_flag=True,
        help=(
            "recreate vector store. use this option if document files were "
            "copied into the content folder or the vector configuration changed."
        ),
    )
    @click.option(
        "--create-tables",
        is_flag=True,
        help="create empty tables if not existed",
    )
    @click.option(
        "--clear-tables",
        is_flag=True,
        help="drop database tables before recreating vector stores",
    )
    @click.option(
        "-u",
        "--update-in-db",
        is_flag=True,
        help="update vectors for files that already exist in the database",
    )
    @click.option(
        "-i",
        "--increment",
        is_flag=True,
        help="add local files that do not yet exist in the database",
    )
    @click.option(
        "--prune-db",
        is_flag=True,
        help="delete database documents whose source file no longer exists",
    )
    @click.option(
        "--prune-folder",
        is_flag=True,
        help="delete source files that no longer exist in the database",
    )
    @click.option(
        "-n",
        "--kb-name",
        multiple=True,
        default=[],
        help="knowledge base names; default is every folder under KB_ROOT_PATH",
    )
    @click.option(
        "-e",
        "--embed-model",
        type=str,
        default=None,
        help="embedding model; the server default is used when omitted",
    )
    @click.option(
        "--system",
        is_flag=True,
        default=False,
        help=(
            "系统维护模式：允许对 OA/工单托管知识库执行重建/更新等维护操作。"
            "仅运维通过 CLI 使用（如重建以应用新字段）；WebUI 仍保持只读保护。"
        ),
    )
    def knowledge_base_command(**kwargs) -> None:
        run_worker(kwargs)

    return knowledge_base_command
