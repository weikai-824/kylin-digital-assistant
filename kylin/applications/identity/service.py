"""身份校验应用服务：工号 → 用户身份（OA 实时查询 / Mock / 本地固定身份）。

长期记忆按「人」（user_id）隔离。

身份模式（oa_settings.identity_mode，可用 KYLIN_IDENTITY_MODE 覆盖）：
- fixed_dev：本地默认，无工号登录，注入固定 userid，跨对话记忆仍可用
- oa_login：侧边栏工号登录；mock_auth 或真实 OA 校验

Mock 模式：工号直接通过，userid=mock_<工号>；凭证到位后关 mock_auth 即走真实 OA。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from kylin.platform.observability.logging import build_logger
from kylin.ports.data.oa_identity import OAIdentityPort

logger = build_logger()

# 同一工号 1 小时内重复校验不打 OA（进程内缓存）
CACHE_TTL_SECONDS = 3600

IDENTITY_MODE_FIXED_DEV = "fixed_dev"
IDENTITY_MODE_OA_LOGIN = "oa_login"


@dataclass(frozen=True, slots=True)
class UserIdentity:
    """一次校验通过后的用户身份（长期记忆按 userid 隔离）。"""

    userid: str
    job_num: str
    username: str
    dept: str = ""
    mock: bool = False


def resolve_identity_mode(settings: Any | None = None) -> str:
    """解析身份模式：环境变量优先；settings 由组合根/适配器层注入。"""
    env = (os.environ.get("KYLIN_IDENTITY_MODE") or "").strip().lower()
    if env in {IDENTITY_MODE_FIXED_DEV, IDENTITY_MODE_OA_LOGIN}:
        return env
    mode = ""
    if settings is not None:
        mode = (getattr(settings, "identity_mode", "") or "").strip().lower()
    if mode not in {IDENTITY_MODE_FIXED_DEV, IDENTITY_MODE_OA_LOGIN}:
        return IDENTITY_MODE_FIXED_DEV
    return mode


def fixed_dev_identity(settings: Any | None = None) -> UserIdentity:
    """本地固定身份：保证有稳定 user_id，长期记忆可测。"""
    userid = (os.environ.get("KYLIN_DEV_USER_ID") or "").strip()
    username = ""
    if not userid and settings is not None:
        userid = (getattr(settings, "fixed_userid", "") or "").strip()
    if not username and settings is not None:
        username = (getattr(settings, "fixed_username", "") or "").strip()
    return UserIdentity(
        userid=userid or "dev_local",
        job_num="dev",
        username=username or "本地开发者",
        dept="",
        mock=True,
    )


def identity_to_session_dict(identity: UserIdentity) -> dict:
    """WebUI session_state['user'] 结构。"""
    return {
        "userid": identity.userid,
        "job_num": identity.job_num,
        "username": identity.username,
        "dept": identity.dept,
        "mock": identity.mock,
    }


class IdentityService:
    """工号校验门面。

    - mock 模式（默认）：工号非空即通过，返回 mock 身份，链路全通
    - 真实模式：调 OA ``queryEmployee`` 校验并解析姓名/部门
    """

    def __init__(self, settings: Any | None = None, oa: OAIdentityPort | None = None):
        """``settings``/``oa`` 由组合根（bootstrap）注入；None 时走 Mock 模式。

        ``oa`` 是 OA 查询端口（真实 OAClient 或测试替身），应用层不感知具体实现。
        """
        self._settings = settings
        self._oa = oa
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, UserIdentity]] = {}

    @property
    def mock_enabled(self) -> bool:
        """OA 凭证未就绪（占位符/未启用/未填 app_key）或显式 mock_auth=true 时走 Mock。"""
        if self._settings is None:
            return True
        if self._settings.mock_auth:
            return True
        app_key = (self._settings.app_key or "").strip()
        if not app_key or "PLACEHOLDER" in app_key.upper():
            return True
        return not (
            self._settings.enabled
            and (self._settings.base_url or "").strip()
            and (self._settings.corpid or "").strip()
        )

    def _get_client(self) -> OAIdentityPort:
        if self._oa is None:
            raise RuntimeError("OA 客户端未注入：请在 bootstrap 组装时传入 oa")
        return self._oa

    def verify(self, job_num: str) -> UserIdentity:
        """校验工号并返回身份；失败抛 ValueError（参数错误）/ RuntimeError（校验失败）。"""
        job_num = (job_num or "").strip()
        if not job_num:
            raise ValueError("工号不能为空")
        now = time.monotonic()
        cached = self._cache.get(job_num)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
        identity = self._verify_uncached(job_num)
        self._cache[job_num] = (now, identity)
        logger.info(
            "身份校验通过 job_num=%s userid=%s mock=%s",
            job_num,
            identity.userid,
            self.mock_enabled,
        )
        return identity

    def _verify_uncached(self, job_num: str) -> UserIdentity:
        if self.mock_enabled:
            return UserIdentity(
                userid=f"mock_{job_num}",
                job_num=job_num,
                username=f"员工{job_num}",
                dept="未同步",
                mock=True,
            )
        try:
            info = self._get_client().query_employee_by_job_num(job_num)
        except Exception as error:  # noqa: BLE001 —— OA 各类网络/接口错误统一转用户可读信息
            logger.warning("OA 身份校验失败 工号=%s: %s", job_num, error)
            raise RuntimeError("工号校验失败，请稍后重试或联系管理员") from error
        return UserIdentity(
            userid=info["userid"],
            job_num=info["job_num"],
            username=info.get("username") or f"员工{job_num}",
            dept=info.get("dept") or "",
            mock=False,
        )
