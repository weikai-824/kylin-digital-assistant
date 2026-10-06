"""OA OAuth adapter."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from cryptography.fernet import Fernet, InvalidToken

from kylin.platform.runtime.net import get_httpx_client
from kylin.platform.runtime.redact import redact
from kylin.platform.observability.logging import build_logger
from kylin.adapters.outbound.oa.config import OASettings, OA_TOKEN_CACHE_PATH
from kylin.platform.config.settings import KYLIN_ROOT

logger = build_logger()


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0

    def is_access_valid(self, skew_seconds: int = 120) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - skew_seconds

    def is_refresh_valid(self, skew_seconds: int = 300) -> bool:
        return bool(self.refresh_token) and time.time() < self.expires_at + 22 * 3600 - skew_seconds


class OAAuth:
    """Obtain and cache OA access tokens."""

    def __init__(self, settings: OASettings, client: Optional[httpx.Client] = None):
        self.settings = settings
        self._client = client or get_httpx_client(timeout=settings.request_timeout)
        self._owns_client = client is None
        self._bundle = load_token_cache()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get_access_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._bundle and self._bundle.is_access_valid():
            return self._bundle.access_token
        if self._bundle and self._bundle.is_refresh_valid():
            try:
                self._bundle = self._refresh(self._bundle.refresh_token)
                save_token_cache(self._bundle)
                return self._bundle.access_token
            except Exception as error:
                logger.warning(f"OA refreshToken 失败，改为重新授权: {error}")
        self._bundle = self._authorize_and_exchange()
        save_token_cache(self._bundle)
        return self._bundle.access_token

    def _authorize_and_exchange(self) -> TokenBundle:
        code = self._get_code()
        return self._exchange_code(code)

    def _get_code(self) -> str:
        url = f"{self.settings.base_url.rstrip('/')}/papi/openapi/oauth2/authorize"
        response = self._client.post(
            url,
            json={
                "corpid": self.settings.corpid,
                "response_type": "code",
                "state": secrets.token_hex(8),
            },
        )
        response.raise_for_status()
        data = response.json()
        self._raise_if_error(data, "获取 code")
        code = data.get("code")
        if not code:
            raise RuntimeError(f"authorize 响应缺少 code: {redact(data)}")
        return code

    def _exchange_code(self, code: str) -> TokenBundle:
        return self._token_request(
            "/papi/openapi/oauth2/access_token",
            {
                "app_key": self.settings.app_key,
                "app_secret": self.settings.app_secret,
                "grant_type": "authorization_code",
                "code": code,
            },
            "获取 accessToken",
        )

    def _refresh(self, refresh_token: str) -> TokenBundle:
        return self._token_request(
            "/papi/openapi/oauth2/refresh_token",
            {
                "app_key": self.settings.app_key,
                "app_secret": self.settings.app_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            "刷新 accessToken",
        )

    def _token_request(self, path: str, payload: dict[str, str], action: str) -> TokenBundle:
        url = f"{self.settings.base_url.rstrip('/')}{path}"
        response = self._client.post(url, json=payload)
        if response.status_code >= 400:
            response = self._client.post(url, data=payload)
        response.raise_for_status()
        data = response.json()
        self._raise_if_error(data, action)
        return self._bundle_from_token_response(data)

    @staticmethod
    def _bundle_from_token_response(data: dict[str, Any]) -> TokenBundle:
        access_token = data.get("accessToken") or data.get("acesstoken") or data.get("access_token")
        if not access_token:
            raise RuntimeError(f"token 响应缺少 accessToken: {redact(data)}")
        return TokenBundle(
            access_token=access_token,
            refresh_token=data.get("refreshToken") or data.get("refresh_token") or "",
            expires_at=time.time() + int(data.get("expires_in") or 7200),
        )

    @staticmethod
    def _raise_if_error(data: dict[str, Any], action: str) -> None:
        error_code = data.get("errcode")
        if error_code is None and isinstance(data.get("message"), dict):
            error_code = data["message"].get("errcode")
        if error_code is not None and str(error_code) not in ("0", ""):
            message = data.get("errmsg")
            if not message and isinstance(data.get("message"), dict):
                message = data["message"].get("errmsg")
            raise RuntimeError(
                f"{action} 失败: errcode={error_code}, errmsg={message}, raw={redact(data)}"
            )


_OA_TOKEN_KEY_ENV = "KYLIN_OA_TOKEN_KEY"
_KEY_MATERIAL = "kylin-oa-token-v1"


def _fernet() -> Fernet:
    """本地派生 Fernet 密钥：环境变量优先，否则由 KYLIN_ROOT 派生。

    单机部署下密钥不落盘（不引入 KMS 复杂度）；多机或容器重建场景可通过
    ``KYLIN_OA_TOKEN_KEY`` 注入固定密钥，保证缓存跨实例可读。
    """
    raw = (os.environ.get(_OA_TOKEN_KEY_ENV) or "").strip()
    if not raw:
        raw = f"{_KEY_MATERIAL}:{KYLIN_ROOT}"
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def load_token_cache(path: Optional[Path] = None) -> Optional[TokenBundle]:
    """读取 token 缓存；密文与旧明文两种格式均支持。

    读到旧明文缓存时会顺手重写为密文，避免明文凭证继续留在磁盘上。
    """
    cache_path = path or OA_TOKEN_CACHE_PATH
    if not cache_path.is_file():
        return None
    try:
        raw_bytes = cache_path.read_bytes()
    except Exception as error:
        logger.warning(f"读取 OA token 缓存失败: {error}")
        return None

    try:
        text = _fernet().decrypt(raw_bytes).decode("utf-8")
    except (InvalidToken, ValueError):
        # 旧格式：明文 JSON。Fernet 带 HMAC 校验，明文必然触发 InvalidToken。
        try:
            text = raw_bytes.decode("utf-8")
            json.loads(text)
        except Exception as error:
            logger.warning(f"读取 OA token 缓存失败: {error}")
            return None
        try:
            cache_path.write_bytes(_fernet().encrypt(text.encode("utf-8")))
            logger.info("已将明文 OA token 缓存升级为加密格式")
        except Exception as error:
            logger.warning(f"OA token 缓存升级为加密格式失败: {error}")

    try:
        raw = json.loads(text)
    except Exception as error:
        logger.warning(f"读取 OA token 缓存失败: {error}")
        return None
    return TokenBundle(
        access_token=raw.get("access_token", ""),
        refresh_token=raw.get("refresh_token", ""),
        expires_at=float(raw.get("expires_at") or 0),
    )


def save_token_cache(bundle: TokenBundle, path: Optional[Path] = None) -> None:
    """加密写 token 缓存（refresh_token 是长期凭证，不可明文落盘）。"""
    cache_path = path or OA_TOKEN_CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(bundle), ensure_ascii=False).encode("utf-8")
    cache_path.write_bytes(_fernet().encrypt(payload))
