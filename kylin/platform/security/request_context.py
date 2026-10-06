"""Request-scoped identity context shared by HTTP adapters and applications."""

from __future__ import annotations

from contextvars import ContextVar

from kylin.platform.security.login_user import LoginUser


_current_user: ContextVar[LoginUser | None] = ContextVar(
    "kylin_current_login_user", default=None
)


def set_current_user(user: LoginUser | None):
    return _current_user.set(user)


def reset_current_user(token) -> None:
    _current_user.reset(token)


def get_current_user() -> LoginUser | None:
    return _current_user.get()
