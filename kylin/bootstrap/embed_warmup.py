"""启动期 embed 模型预热：避免首次 kb_chat 被 20s+ 冷探拖死。

调用 ``schedule_embed_warmup()`` 后立即返回；实际探测在独立线程里跑。
任何异常只 warn，不影响服务启动——缓存未填的代价由 ``ensure_ready`` 兜底。

作用链：
    startup -> schedule_embed_warmup() -> ThreadPoolExecutor -> embed_query("warmup")
        -> Ollama/远程 embed 端加载权重（或预热连接池）
        -> 主进程后续第一个 kb_chat 进入 ensure_ready 时缓存仍为空，
           但 embed 端已热，第二条开始就回到正常时延。

注意：``ensure_ready`` 的缓存是**当前进程**模块级 dict，且 API 子进程是 spawn 模式，
因此本预热必须在 API 子进程内调用，而不是 start_main_server 父进程。
``run_api_server`` 即调用点。
"""

from __future__ import annotations

import logging
import threading

from kylin.platform.config.settings import Settings


logger = logging.getLogger(__name__)

_warmup_lock = threading.Lock()
_warmup_scheduled = False


def schedule_embed_warmup() -> None:
    """预约一次后台 embed 探测。已预约则幂等跳过。

    线程数 1 够用——只是把模型/page-in/连接预热一次，并发探测多个 KB 反而会互相挤兑。
    """
    global _warmup_scheduled
    with _warmup_lock:
        if _warmup_scheduled:
            return
        _warmup_scheduled = True

    embed_model = Settings.model_settings.DEFAULT_EMBEDDING_MODEL
    t = threading.Thread(
        target=_probe_embed,
        args=(embed_model,),
        name="embed-warmup",
        daemon=True,
    )
    t.start()


def _probe_embed(embed_model: str) -> None:
    """真正的探测：失败只 warn，不抛。"""
    try:
        from kylin.adapters.outbound.model.clients import get_Embeddings  # 延迟导入
        embeddings = get_Embeddings(embed_model=embed_model)
        # 单条最短输入触发一次往返：Ollama 会 page-in 权重；远端 embed 会建连
        embeddings.embed_query("warmup")
        logger.info("embed 模型 %s 预热完成", embed_model)
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "embed 模型 %s 预热失败（首次 kb_chat 可能慢）：%s", embed_model, error
        )
