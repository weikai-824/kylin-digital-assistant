"""OA 同步应用的 Click 入站适配器。"""

from __future__ import annotations

import logging
import signal

import click

from kylin.applications.knowledge_ingestion.oa_schedule import (
    request_stop,
    run_oa_sync_schedule,
)

logger = logging.getLogger(__name__)


def create_oa_sync_command(build_service, settings_path, initialize_settings, load_settings):
    """创建由组合根注入依赖的 OA 同步命令。"""

    @click.command("oa-sync", help="从 OA 拉取知识文档并写入本地知识库（一期）")
    @click.option("--full", is_flag=True, default=False, help="全量重同步（忽略本地增量状态）")
    @click.option("--dry-run", is_flag=True, default=False, help="只拉取列表并打印决策，不下载不入库")
    @click.option("--limit", default=0, show_default=True, help="最多处理 N 篇变更文档，0 表示不限制")
    @click.option(
        "--init-config",
        is_flag=True,
        default=False,
        help=f"在 KYLIN_ROOT 生成 oa_settings.yaml 模板（{settings_path}）",
    )
    @click.option(
        "--retry-failed",
        is_flag=True,
        default=False,
        help="只重试 state 中 last_error 非空的失败文档（不重跑已成功的）",
    )
    @click.option(
        "--daemon",
        is_flag=True,
        default=False,
        help="常驻周期同步（读 schedule_enabled / interval_hours；不可与 --full/--dry-run/--limit/--retry-failed 同用）",
    )
    def oa_sync_command(
        full: bool,
        dry_run: bool,
        limit: int,
        init_config: bool,
        retry_failed: bool,
        daemon: bool,
    ) -> None:
        if init_config:
            click.echo(f"已生成/保留配置文件: {initialize_settings()}")
            return
        if not settings_path.is_file():
            initialize_settings()
            click.echo(f"未找到配置，已生成模板: {settings_path}")
            click.echo("请填写 corpid/app_key/app_secret/userid 后，将 enabled 设为 true，再重新执行。")
            return

        if daemon:
            if full or dry_run or limit or retry_failed:
                raise click.UsageError(
                    "--daemon 不可与 --full / --dry-run / --limit / --retry-failed 同时使用"
                )
            _install_stop_signals()
            try:
                run_oa_sync_schedule(
                    load_settings=load_settings,
                    build_service=build_service,
                )
            except ValueError as error:
                raise click.ClickException(str(error)) from error
            return

        service = build_service()
        only_ids = None
        if retry_failed and not full:
            failed_ids = service.failed_document_ids()
            if not failed_ids:
                click.echo("没有失败文档可重试")
                return
            click.echo(f"将重试 {len(failed_ids)} 篇失败文档: {failed_ids}")
            only_ids = set(failed_ids)

        result = service.sync(
            full=full,
            dry_run=dry_run,
            limit=limit,
            only_ids=only_ids,
        )
        click.echo(result.to_dict())

    return oa_sync_command


def _install_stop_signals() -> None:
    def _handler(signum, _frame) -> None:
        logger.info("收到信号 %s，将在当前检查点停止 OA daemon", signum)
        request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass
