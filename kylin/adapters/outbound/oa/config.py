"""OA configuration file adapter."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Optional

from kylin.platform.config.pydantic_settings_file import (
    BaseFileSettings,
    Field,
    SettingsConfigDict,
    import_yaml,
    settings_property,
)
from kylin.platform.config.settings import KYLIN_ROOT


OA_SETTINGS_PATH = KYLIN_ROOT / "oa_settings.yaml"
OA_TOKEN_CACHE_PATH = KYLIN_ROOT / "data" / "oa_token_cache.json"
OA_SYNC_STATE_PATH = KYLIN_ROOT / "data" / "oa_sync_state.json"


class OASettings(BaseFileSettings):
    """Configuration required by the existing OA synchronization flow."""

    model_config = SettingsConfigDict(yaml_file=OA_SETTINGS_PATH)

    enabled: bool = False
    identity_mode: str = "fixed_dev"
    """WebUI 身份模式：fixed_dev=无登录固定身份；oa_login=侧边栏工号登录。"""
    fixed_userid: str = "dev_local"
    """fixed_dev 模式下写入长期记忆的稳定 userid；可用 KYLIN_DEV_USER_ID 覆盖。"""
    fixed_username: str = "本地开发者"
    mock_auth: bool = False
    """身份校验 Mock 模式：OA 凭证未配置/不可用时，工号直接通过（userid=mock_<工号>，部门=未同步）。
    仅 identity_mode=oa_login 时有意义；凭证就绪后可置 false 走真实 OA 校验。"""
    base_url: str = "http://127.0.0.1:10600"
    corpid: str = ""
    app_key: str = ""
    app_secret: str = ""
    userid: str = ""
    job_num: str = ""
    account: str = ""
    kb_name: str = "oa_kb"
    kb_info: str = "OA 知识文档同步库"
    page_size: int = 50
    folder_path_whitelist: list[str] = Field(default_factory=list)
    skip_status_not_normal: bool = True
    ingest_html_content: bool = True
    request_timeout: float = 60.0
    download_module: str = ""
    auth_in_header: bool = False
    """OA 鉴权传递方式。

    false（默认，与既有行为一致）：token 走 URL query ``?access_token=...``，
    缺点是会进网关访问日志与代理日志。
    true：改用 ``Authorization: Bearer`` 请求头，不落访问日志。

    ⚠️ 置为 true 前必须先与 OA 网关联调确认其接受 Bearer 头；若网关只认
    query 参数，置 true 会导致所有 OA 请求鉴权失败（改回 false 即恢复）。
    """
    # 周期调度（kylin oa-sync --daemon）；与一次性 oa-sync 独立
    schedule_enabled: bool = False
    interval_hours: int = Field(default=24, ge=1)
    run_on_start: bool = True
    document_public_by_default: bool = False
    """OA 文档访问范围默认策略。

    false（默认）：OA 文档默认仅所属部门可见（access_depts=[文档部门]），
    保持部门隔离；部分不敏感部门文档需要开放给其他部门时，可全局置 true
    让所有 OA 文档默认全开放（access_depts 留空）。
    """


_OA_CACHED = settings_property(OASettings())


def load_oa_settings(path: Optional[Path] = None) -> OASettings:
    """Load OA settings, falling back to defaults when the file is absent."""
    if path is not None:
        if not path.is_file():
            return OASettings()
        yaml = import_yaml()
        with open(path, "r", encoding="utf-8") as file:
            return OASettings.model_validate(yaml.load(file) or {})
    try:
        return _OA_CACHED()
    except Exception:
        return OASettings()


def save_oa_settings_template(path: Optional[Path] = None, overwrite: bool = False) -> Path:
    """Create the existing OA settings template if it does not exist."""
    config_path = path or OA_SETTINGS_PATH
    if config_path.is_file() and not overwrite:
        return config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    yaml = import_yaml()
    buffer = StringIO()
    yaml.dump(OASettings().model_dump(), buffer)
    comment = (
        "# OA 知识文档同步配置（一期）\n"
        "# 填写 base_url / corpid / app_key / app_secret / userid（或 job_num）后，\n"
        "# 将 enabled 改为 true，再执行: kylin oa-sync\n"
        "# 周期拉取: schedule_enabled=true 后执行 kylin oa-sync --daemon\n"
        "# 本文件含密钥，勿提交 git。\n"
    )
    with open(config_path, "w", encoding="utf-8") as file:
        file.write(comment + buffer.getvalue())
    return config_path
