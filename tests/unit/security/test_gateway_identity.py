from __future__ import annotations

import json
from urllib.parse import quote_plus

import pytest

from kylin.platform.security.login_user import LoginUser


def test_login_user_decodes_java_gateway_header() -> None:
    header = quote_plus(
        json.dumps(
            {
                "id": 10001,
                "userType": 1,
                "tenantId": 1,
                "scopes": ["ai:digital-human:chat"],
                "clientId": "web",
                "info": {"deptId": "10"},
            }
        )
    )

    user = LoginUser.from_header(header)

    assert user.id == 10001
    assert user.tenant_id == 1
    assert user.scopes == ("ai:digital-human:chat",)
    assert user.info["deptId"] == "10"


@pytest.mark.parametrize("payload", ["", "not-json", quote_plus("[]")])
def test_login_user_rejects_invalid_header(payload: str) -> None:
    with pytest.raises((ValueError, TypeError, json.JSONDecodeError)):
        LoginUser.from_header(payload)
