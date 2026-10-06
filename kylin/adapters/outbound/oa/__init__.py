"""OA external-system adapters."""

from kylin.adapters.outbound.oa.client import OAClient
from kylin.adapters.outbound.oa.config import (
    OA_SETTINGS_PATH,
    OASettings,
    load_oa_settings,
    save_oa_settings_template,
)
from kylin.adapters.outbound.oa.state import JsonOASyncStateStore

__all__ = [
    "OAClient",
    "OASettings",
    "OA_SETTINGS_PATH",
    "JsonOASyncStateStore",
    "load_oa_settings",
    "save_oa_settings_template",
]
