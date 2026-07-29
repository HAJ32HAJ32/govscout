from __future__ import annotations

import base64
from collections.abc import Mapping
import os
from pathlib import Path

from flask import Flask

from govscout.auth import AuthConfig, PUBLIC_HOST
from govscout.config import load_default_settings, load_settings
from govscout.db import connect_database, migrate
from govscout.sendguard import SendGuard
from govscout.web.app import create_app
from govscout.web_hosts import canonical_safe_bind_host


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value:
        raise ValueError(f"missing required public deployment setting: {name}")
    return value


def _session_secret(encoded: str) -> bytes:
    try:
        secret = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("GOVSCOUT_SESSION_SECRET must be valid base64url") from exc
    if len(secret) < 32:
        raise ValueError("GOVSCOUT_SESSION_SECRET must decode to at least 32 bytes")
    return secret


def create_production_app(environment: Mapping[str, str] | None = None) -> Flask:
    """Create the fail-closed public WSGI app for Gunicorn behind the Caddy proxy."""
    env = os.environ if environment is None else environment
    if _required(env, "GOVSCOUT_DEPLOYMENT_MODE") != "public-proxy":
        raise ValueError("production WSGI requires GOVSCOUT_DEPLOYMENT_MODE=public-proxy")

    bind_host = _required(env, "GOVSCOUT_BIND_HOST")
    try:
        canonical_bind = canonical_safe_bind_host(bind_host)
    except ValueError as exc:
        raise ValueError("public proxy bind must be a loopback IP literal") from exc
    if canonical_bind != "127.0.0.1":
        raise ValueError("public proxy bind must be exactly 127.0.0.1")

    public_host = _required(env, "GOVSCOUT_PUBLIC_HOST")
    if public_host != PUBLIC_HOST:
        raise ValueError(f"GOVSCOUT_PUBLIC_HOST must be exactly {PUBLIC_HOST}")

    auth = AuthConfig(
        username=_required(env, "GOVSCOUT_USERNAME"),
        password_hash=_required(env, "GOVSCOUT_PASSWORD_HASH"),
        session_secret=_session_secret(_required(env, "GOVSCOUT_SESSION_SECRET")),
        public_host=public_host,
        public_https=True,
    )
    database = Path(_required(env, "GOVSCOUT_DATABASE"))
    settings_path = env.get("GOVSCOUT_CONFIG")
    settings = load_settings(settings_path) if settings_path else load_default_settings()

    setup = connect_database(database)
    try:
        migrate(setup)
    finally:
        setup.close()

    return create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(settings),
        trusted_hosts=(canonical_bind,),
        auth=auth,
    )


def application_factory() -> Flask:
    """Zero-argument Gunicorn factory entrypoint."""
    return create_production_app()
