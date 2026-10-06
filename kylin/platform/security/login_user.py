"""Java Gateway ``login-user`` header compatibility model."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote_plus


@dataclass(frozen=True)
class LoginUser:
    """The user context forwarded by the existing Java Gateway."""

    id: int
    user_type: int | None = None
    tenant_id: int | None = None
    scopes: tuple[str, ...] = ()
    client_id: str | None = None
    expires_time: str | None = None
    info: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_header(cls, value: str) -> "LoginUser":
        """Decode the URL-encoded JSON emitted by ``SecurityFrameworkUtils``."""
        payload = json.loads(unquote_plus(value))
        if not isinstance(payload, dict) or payload.get("id") is None:
            raise ValueError("login-user header is missing user id")
        scopes = payload.get("scopes") or []
        if not isinstance(scopes, list):
            raise ValueError("login-user scopes must be a list")
        info = payload.get("info") or {}
        if not isinstance(info, dict):
            raise ValueError("login-user info must be an object")
        return cls(
            id=int(payload["id"]),
            user_type=payload.get("userType"),
            tenant_id=payload.get("tenantId"),
            scopes=tuple(str(item) for item in scopes),
            client_id=payload.get("clientId"),
            expires_time=payload.get("expiresTime"),
            info=info,
        )
