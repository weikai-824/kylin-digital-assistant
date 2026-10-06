"""异步/线程池执行工具（共享基础设施）。

当前供迁移中的数据实现和运行时任务复用。
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import logging
import multiprocessing as mp
import sys
from typing import Callable, Dict, Generator, List


logger = logging.getLogger("kylin")


def run_async(cor):
    """
    在同步环境中运行异步代码.
    """
    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except Exception:
        loop = asyncio.new_event_loop()
    return loop.run_until_complete(cor)


def iter_over_async(ait, loop=None):
    """
    将异步生成器封装成同步生成器.
    """
    import asyncio

    ait = ait.__aiter__()

    async def get_next():
        try:
            obj = await ait.__anext__()
            return False, obj
        except StopAsyncIteration:
            return True, None

    if loop is None:
        try:
            loop = asyncio.get_event_loop()
        except Exception:
            loop = asyncio.new_event_loop()

    while True:
        done, obj = loop.run_until_complete(get_next())
        if done:
            break
        yield obj


async def wrap_done(fn, event):
    """Wrap an awaitable with a event to signal when it's done or an exception is raised."""
    import logging

    logger = logging.getLogger("kylin")
    try:
        await fn
    except Exception as e:
        logger.error(f"{e.__class__.__name__}: Caught exception: {e}")
    finally:
        event.set()


def run_in_thread_pool(
        func: Callable,
        params: List[Dict] = [],
) -> Generator:
    """
    在线程池中批量运行任务，并将运行结果以生成器的形式返回。
    请确保任务中的所有操作是线程安全的，任务函数请全部使用关键字参数。
    """
    tasks = []
    with ThreadPoolExecutor() as pool:
        for kwargs in params:
            tasks.append(pool.submit(func, **kwargs))

        for obj in as_completed(tasks):
            try:
                yield obj.result()
            except Exception as e:
                logger.exception(f"error in sub thread: {e}")


def run_in_process_pool(
        func: Callable,
        params: List[Dict] = [],
) -> Generator:
    """
    在线程池中批量运行任务，并将运行结果以生成器的形式返回。
    请确保任务中的所有操作是线程安全的，任务函数请全部使用关键字参数。
    """
    tasks = []
    max_workers = None
    if sys.platform.startswith("win"):
        max_workers = min(
            mp.cpu_count(), 60
        )  # max_workers should not exceed 60 on windows
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        for kwargs in params:
            tasks.append(pool.submit(func, **kwargs))

        for obj in as_completed(tasks):
            try:
                yield obj.result()
            except Exception as e:
                logger.exception(f"error in sub process: {e}")
