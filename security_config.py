"""Fail-closed Flask session security configuration.

Deployments must provide ``FLASK_SECRET_KEY``. Generate one with, for example,
``python -c "import secrets; print(secrets.token_urlsafe(48))"`` and keep it out
of source control.

Session cookies are Secure by default. For local HTTP-only development, set
``FLASK_SESSION_COOKIE_SECURE=false`` explicitly. Never use that override in a
deployed environment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from flask import Flask


_MINIMUM_SECRET_LENGTH = 32
_RETIRED_SECRETS = frozenset({"your_super_secret_key"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _secret_from_environment(environment: Mapping[str, str]) -> str:
    secret = environment.get("FLASK_SECRET_KEY", "")
    if not secret:
        raise RuntimeError("FLASK_SECRET_KEY is required")
    if secret in _RETIRED_SECRETS:
        raise RuntimeError("FLASK_SECRET_KEY uses a retired, compromised value")
    if len(secret.encode("utf-8")) < _MINIMUM_SECRET_LENGTH:
        raise RuntimeError("FLASK_SECRET_KEY must contain at least 32 bytes")
    if len(set(secret)) < 12:
        raise RuntimeError("FLASK_SECRET_KEY does not have enough variation")
    return secret


def _secure_cookie_setting(environment: Mapping[str, str]) -> bool:
    value = environment.get("FLASK_SESSION_COOKIE_SECURE")
    if value is None:
        return True

    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RuntimeError(
        "FLASK_SESSION_COOKIE_SECURE must be a boolean value "
        "(true/false, yes/no, on/off, or 1/0)"
    )


def configure_security(app: Flask) -> None:
    """Apply required signing-key and secure session-cookie settings to *app*.

    This must run during application startup, before the app serves requests.
    Invalid or missing security configuration raises ``RuntimeError`` so the
    process cannot silently start with an unsafe fallback.
    """

    app.config.update(
        SECRET_KEY=_secret_from_environment(os.environ),
        SECRET_KEY_FALLBACKS=None,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_secure_cookie_setting(os.environ),
    )
