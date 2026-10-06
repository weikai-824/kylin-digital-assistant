"""服务启动组合根：以多进程启动 FastAPI 与 Streamlit 入口。"""

import asyncio
import logging
import logging.config
import multiprocessing as mp
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import click
from fastapi import FastAPI

from kylin.platform.observability.logging import build_logger


logger = build_logger()
STARTUP_TIMEOUT_SECONDS = 60


def _is_dev_mode() -> bool:
    return os.environ.get("KYLIN_DEV", "").strip().lower() in ("1", "true", "yes")


def _kylin_source_dir() -> str:
    return str(Path(__file__).resolve().parents[1])


def _set_app_event(app: FastAPI, started_event: mp.Event = None):
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if started_event is not None:
            started_event.set()
        if original_lifespan is not None:
            async with original_lifespan(app) as state:
                yield state
        else:
            yield

    app.router.lifespan_context = lifespan

def run_api_server(
    started_event: mp.Event = None, run_mode: str = None
):
    """以 uvicorn 启动 FastAPI API 服务（:7861）。"""
    # 必须在 create_app / pymilvus 导入之前补 marshmallow 版本属性。
    from kylin.platform.runtime.compat_patches import apply_compat_patches

    apply_compat_patches()

    import uvicorn
    from kylin.platform.observability.logging import (
        get_config_dict,
        get_log_file,
        get_timestamp_ms,
    )

    from kylin.platform.config.settings import Settings
    from kylin.bootstrap.http import create_app
    from kylin.bootstrap.embed_warmup import schedule_embed_warmup
    from kylin.platform.runtime.net import set_httpx_config

    logger.info(f"Api MODEL_PLATFORMS: {Settings.model_settings.MODEL_PLATFORMS}")
    set_httpx_config()

    # P0：API 子进程内异步预热默认 embed 模型，避免 kb_chat 第一次请求被 20s 冷探拖。
    # 必须在 spawn 后的子进程调用（_ready_cache 是模块级 dict，父进程不带到子进程）。
    # 不阻塞 uvicorn.run：探测在 daemon 线程里跑，失败只 warn。
    schedule_embed_warmup()

    host = Settings.basic_settings.API_SERVER["host"]
    port = Settings.basic_settings.API_SERVER["port"]

    logging_conf = get_config_dict(
        "INFO",
        get_log_file(log_path=Settings.basic_settings.LOG_PATH, sub_dir=f"run_api_server_{get_timestamp_ms()}"),
        1024 * 1024 * 1024 * 3,
        1024 * 1024 * 1024 * 3,
    )
    logging.config.dictConfig(logging_conf)  # type: ignore

    if _is_dev_mode():
        logger.info("KYLIN_DEV=1: API 热重载已开启（watch %s）", _kylin_source_dir())
        if started_event is not None:
            started_event.set()
        uvicorn.run(
            "kylin.bootstrap.http:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            reload_dirs=[_kylin_source_dir()],
        )
        return

    app = create_app(run_mode=run_mode)
    _set_app_event(app, started_event)
    uvicorn.run(app, host=host, port=port)


def run_webui(
    started_event: mp.Event = None, run_mode: str = None
):
    """WebUI 入站适配器入口（:8501）。

    批次 1 起默认启动 NiceGUI 调试台（webui_ng）；
    设 ``KYLIN_WEBUI_LEGACY=1`` 回退旧 Streamlit 版（webui），用于对照/回滚，
    回退只需改环境变量 + restart，无需 rebuild。批次 3 下线 Streamlit 时
    将删除 _run_webui_streamlit 与相关依赖。
    """
    import os

    legacy = os.environ.get("KYLIN_WEBUI_LEGACY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if legacy:
        logger.info("KYLIN_WEBUI_LEGACY=1: 启动旧版 Streamlit WebUI")
        _run_webui_streamlit(started_event=started_event, run_mode=run_mode)
    else:
        _run_webui_nicegui(started_event=started_event, run_mode=run_mode)


def _run_webui_nicegui(
    started_event: mp.Event = None, run_mode: str = None
):
    """以 uvicorn 启动 NiceGUI 调试台（:8501）。

    与 API 子进程同一套路：dev 模式走 uvicorn reload + 工厂；
    Windows bind-mount 下 inotify 不传播，通过 WATCHFILES_FORCE_POLLING
    让 watchfiles 轮询 mtime（等价于旧 Streamlit 的 poll 模式）。
    """
    import os

    from kylin.platform.config.settings import Settings
    from kylin.platform.runtime.net import set_httpx_config
    from kylin.platform.observability.logging import (
        get_config_dict,
        get_log_file,
        get_timestamp_ms,
    )

    logger.info(f"Webui MODEL_PLATFORMS: {Settings.model_settings.MODEL_PLATFORMS}")
    set_httpx_config()

    host = Settings.basic_settings.WEBUI_SERVER["host"]
    port = Settings.basic_settings.WEBUI_SERVER["port"]

    logging.config.dictConfig(
        get_config_dict(
            "INFO",
            get_log_file(
                log_path=Settings.basic_settings.LOG_PATH,
                sub_dir=f"run_webui_{get_timestamp_ms()}",
            ),
            1024 * 1024 * 1024 * 3,
            1024 * 1024 * 1024 * 3,
        )
    )

    from kylin.platform.runtime.compat_patches import apply_compat_patches

    apply_compat_patches()

    import uvicorn

    if _is_dev_mode():
        # 9p/gRPC-FUSE 挂载不传播 inotify：强制 watchfiles 轮询，改代码才能触发重载
        os.environ.setdefault("WATCHFILES_FORCE_POLLING", "true")
        # loguru 不支持 %-style 惰性格式化，一律 f-string
        logger.info(
            f"KYLIN_DEV=1: WebUI 热重载已开启（watchfiles poll; {_kylin_source_dir()}）"
        )
        started_event.set()
        uvicorn.run(
            "kylin.adapters.inbound.webui_ng.main:create_webui_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            reload_dirs=[_kylin_source_dir()],
        )
        return

    from kylin.adapters.inbound.webui_ng import create_webui_app

    app = create_webui_app()
    _set_app_event(app, started_event)
    uvicorn.run(app, host=host, port=port)


def _run_webui_streamlit(
    started_event: mp.Event = None, run_mode: str = None
):
    """以 Streamlit 启动旧版 WebUI（:8501）。批次 3 下线时删除本函数。"""
    import sys
    from pathlib import Path

    from kylin.platform.config.settings import Settings
    from kylin.platform.runtime.net import set_httpx_config
    from kylin.platform.observability.logging import get_config_dict, get_log_file, get_timestamp_ms

    logger.info(f"Webui MODEL_PLATFORMS: {Settings.model_settings.MODEL_PLATFORMS}")
    set_httpx_config()

    host = Settings.basic_settings.WEBUI_SERVER["host"]
    port = Settings.basic_settings.WEBUI_SERVER["port"]
    # 0.0.0.0 只能作为监听地址；Streamlit 自动开浏览器时若用该地址会打不开。
    browser_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    script_dir = str(
        Path(__file__).resolve().parents[1]
        / "adapters"
        / "inbound"
        / "webui"
        / "app.py"
    )
    if not os.path.isfile(script_dir):
        raise FileNotFoundError(f"WebUI 入口不存在: {script_dir}")

    dev_mode = _is_dev_mode()
    if dev_mode:
        logger.info(
            "KYLIN_DEV=1: WebUI 文件监听已开启（poll；Windows 目录 bind mount "
            "不传播 inotify 事件，auto/watchdog 模式在容器内静默失效）"
        )

    flag_options = {
        "server_address": host,
        "server_port": port,
        "theme_base": "light",
        "theme_primaryColor": "#2f7f8a",
        "theme_secondaryBackgroundColor": "#eef3f6",
        "theme_textColor": "#15202b",
        "global_disableWatchdogWarning": None,
        "global_disableWidgetStateDuplicationWarning": None,
        "global_showWarningOnDirectExecution": None,
        "global_developmentMode": None,
        "global_logLevel": None,
        "global_unitTest": None,
        "global_suppressDeprecationWarnings": None,
        "global_minCachedMessageSize": None,
        "global_maxCachedMessageAge": None,
        "global_storeCachedForwardMessagesInMemory": None,
        "global_dataFrameSerialization": None,
        "logger_level": None,
        "logger_messageFormat": None,
        "logger_enableRich": None,
        "client_caching": None,
        "client_displayEnabled": None,
        "client_showErrorDetails": None,
        "client_toolbarMode": None,
        "client_showSidebarNavigation": None,
        "runner_magicEnabled": None,
        "runner_installTracer": None,
        "runner_fixMatplotlib": None,
        "runner_postScriptGC": None,
        "runner_fastReruns": None,
        "runner_enforceSerializableSessionState": None,
        "runner_enumCoercion": None,
        "server_folderWatchBlacklist": None,
        # poll：宿主机目录通过 9p/gRPC-FUSE 挂进容器时 inotify 事件不会传播，
        # auto（watchdog）模式收不到任何变更通知 → 改代码页面永远不刷新。轮询 mtime
        # 是这种挂载方式下唯一可靠的热重载方案。
        # 注意：合法取值只有 auto / watchdog / poll / none。写成 "polling" 会落到
        # get_path_watcher_class 的 else 分支 → NoOpPathWatcher，等于彻底关闭监听。
        # 配合下面的 runOnSave，改完 .py 保存即可自动重跑脚本，无需手动重启容器。
        "server_fileWatcherType": "poll" if dev_mode else "none",
        "server_headless": None,
        "server_runOnSave": dev_mode or None,
        "server_allowRunOnSave": dev_mode or None,
        "server_scriptHealthCheckEnabled": None,
        "server_baseUrlPath": None,
        "server_enableCORS": None,
        "server_enableXsrfProtection": None,
        "server_maxUploadSize": None,
        "server_maxMessageSize": None,
        "server_enableArrowTruncation": None,
        "server_enableWebsocketCompression": None,
        "server_enableStaticServing": None,
        "browser_serverAddress": browser_host,
        "browser_gatherUsageStats": None,
        "browser_serverPort": port,
        "server_sslCertFile": None,
        "server_sslKeyFile": None,
        "ui_hideTopBar": None,
        "ui_hideSidebarNav": None,
        "magic_displayRootDocString": None,
        "magic_displayLastExprIfNoSemicolon": None,
        "deprecation_showfileUploaderEncoding": None,
        "deprecation_showImageFormat": None,
        "deprecation_showPyplotGlobalUse": None,
        "theme_backgroundColor": None,
        "theme_font": None,
    }

    args = []
    if run_mode == "lite":
        args += [
            "--",
            "lite",
        ]

    from kylin.platform.runtime.compat_patches import apply_compat_patches

    apply_compat_patches()

    try:
        # for streamlit >= 1.12.1
        from streamlit.web import bootstrap
    except ImportError:
        from streamlit import bootstrap

    logging_conf = get_config_dict(
        "INFO",
        get_log_file(log_path=Settings.basic_settings.LOG_PATH, sub_dir=f"run_webui_{get_timestamp_ms()}"),
        1024 * 1024 * 1024 * 3,
        1024 * 1024 * 1024 * 3,
    )
    logging.config.dictConfig(logging_conf)  # type: ignore
    bootstrap.load_config_options(flag_options=flag_options)
    # P1 死锁修复：bootstrap.run 是阻塞调用，started_event 必须前置，
    # 否则 -w/--all 模式等待 webui 就绪会永久卡死。
    started_event.set()
    bootstrap.run(script_dir, False, args, flag_options)


def dump_server_info(after_start=False, args=None):
    import platform

    from kylin import __version__
    from kylin.platform.config.settings import Settings
    from kylin.platform.runtime.net import api_address, webui_address

    print("\n")
    print("=" * 30 + " 系统咨询数字助手 " + "=" * 30)
    print(f"操作系统：{platform.platform()}.")
    print(f"python版本：{sys.version}")
    print(f"项目版本：{__version__}")
    print(f"数据目录：{Settings.KYLIN_ROOT}")
    print("\n")

    print(f"当前使用的分词器：{Settings.kb_settings.TEXT_SPLITTER_NAME}")

    print(f"默认选用的 Embedding 名称： {Settings.model_settings.DEFAULT_EMBEDDING_MODEL}")

    if after_start:
        print("\n")
        print(f"服务端运行信息：")
        if args.api:
            print(f"    API Server: {api_address()}")
        if args.webui:
            print(f"    WebUI Server: {webui_address()}")
    print("=" * 30 + " 系统咨询数字助手 " + "=" * 30)
    print("\n")


async def start_main_server(args):
    import signal
    import time

    from kylin.platform.observability.logging import (
        get_config_dict,
        get_log_file,
        get_timestamp_ms,
    )

    from kylin.platform.config.settings import Settings

    logging_conf = get_config_dict(
        "INFO",
        get_log_file(
            log_path=Settings.basic_settings.LOG_PATH, sub_dir=f"start_main_server_{get_timestamp_ms()}"
        ),
        1024 * 1024 * 1024 * 3,
        1024 * 1024 * 1024 * 3,
    )
    logging.config.dictConfig(logging_conf)  # type: ignore

    def handler(signalname):
        """
        Python 3.9 has `signal.strsignal(signalnum)` so this closure would not be needed.
        Also, 3.8 includes `signal.valid_signals()` that can be used to create a mapping for the same purpose.
        """

        def f(signal_received, frame):
            raise KeyboardInterrupt(f"{signalname} received")

        return f

    # This will be inherited by the child process if it is forked (not spawned)
    signal.signal(signal.SIGINT, handler("SIGINT"))
    signal.signal(signal.SIGTERM, handler("SIGTERM"))

    spawn_context = mp.get_context("spawn")
    manager = spawn_context.Manager()
    run_mode = None

    if args.all:
        args.api = True
        args.webui = True

    dump_server_info(args=args)

    if len(sys.argv) > 1:
        logger.info(f"正在启动服务：")
        logger.info(f"如需查看 llm_api 日志，请前往 {Settings.basic_settings.LOG_PATH}")

    processes = {}

    def process_count():
        return len(processes)

    api_started = manager.Event()
    if args.api:
        process = spawn_context.Process(
            target=run_api_server,
            name=f"API Server",
            kwargs=dict(
                started_event=api_started,
                run_mode=run_mode,
            ),
            daemon=False,
        )
        processes["api"] = process

    webui_started = manager.Event()
    if args.webui:
        process = spawn_context.Process(
            target=run_webui,
            name=f"WEBUI Server",
            kwargs=dict(
                started_event=webui_started,
                run_mode=run_mode,
            ),
            # daemon=False：dev 模式 uvicorn reload 需要 spawn 孙进程，
            # daemon 进程不允许有子进程（AssertionError）。与 API 子进程同策略，
            # 生命周期由下方 join/kill 统一管理。
            daemon=False,
        )
        processes["webui"] = process

    try:
        # Windows bind-mount（9p）下 Streamlit 冷启动常超过 60s；开发模式放宽，避免误杀。
        ready_timeout = 180 if _is_dev_mode() else STARTUP_TIMEOUT_SECONDS

        if process := processes.get("api"):
            process.start()
            if not api_started.wait(timeout=ready_timeout):
                raise RuntimeError("API server did not report ready before the startup timeout")

        if process := processes.get("webui"):
            process.start()
            if not webui_started.wait(timeout=ready_timeout):
                raise RuntimeError("WebUI server did not report ready before the startup timeout")

        dump_server_info(after_start=True, args=args)

        # 等待所有进程退出
        while processes:
            for process_name, process in list(processes.items()):
                process.join(2)
                if process.is_alive():
                    continue

                exit_code = process.exitcode
                processes.pop(process_name, None)
                if exit_code:
                    raise RuntimeError(
                        f"{process_name} server exited with status {exit_code}"
                    )
    except KeyboardInterrupt:
        logger.warning("Caught KeyboardInterrupt! Setting stop event...")
    except Exception:
        logger.exception("A managed server exited unexpectedly")
        raise
    finally:
        for process_name, process in list(processes.items()):
            if process.is_alive():
                logger.warning("Sending SIGKILL to %s", process_name)
                process.kill()
            process.join(timeout=5)
            logger.info("Process %s status: %s", process_name, process.exitcode)
            # Queues and other inter-process communication primitives can break when
            # process is killed, but we don't care here
        processes.clear()
        manager.shutdown()


@click.command(help="启动服务")
@click.option(
    "-a",
    "--all",
    "all",
    is_flag=True,
    help="run api.py and webui.py",
)
@click.option(
    "--api",
    "api",
    is_flag=True,
    help="run api.py",
)
@click.option(
    "-w",
    "--webui",
    "webui",
    is_flag=True,
    help="run webui.py server",
)
def main(all, api, webui):
    """CLI start 命令入口：按参数多进程拉起 API 与 WebUI。"""
    class args:
        ...
    args.all = all
    args.api = api
    args.webui = webui

    # 添加这行代码
    cwd = os.getcwd()
    sys.path.append(cwd)
    mp.freeze_support()
    print("cwd:" + cwd)
    from kylin.bootstrap.knowledge_base_migration import create_tables

    create_tables()
    if sys.version_info < (3, 10):
        loop = asyncio.get_event_loop()
    else:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()

        asyncio.set_event_loop(loop)
    loop.run_until_complete(start_main_server(args))


if __name__ == "__main__":
    main()
