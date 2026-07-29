from __future__ import annotations

import base64

import pytest

from govscout.auth import hash_password
from govscout.wsgi import create_production_app


PUBLIC_HOST = "leads.misegroup.co.uk"


def _environment(tmp_path):
    return {
        "GOVSCOUT_DEPLOYMENT_MODE": "public-proxy",
        "GOVSCOUT_BIND_HOST": "127.0.0.1",
        "GOVSCOUT_PUBLIC_HOST": PUBLIC_HOST,
        "GOVSCOUT_USERNAME": "operator",
        "GOVSCOUT_PASSWORD_HASH": hash_password(
            "test-password", salt=b"0123456789abcdef"
        ),
        "GOVSCOUT_SESSION_SECRET": base64.urlsafe_b64encode(b"s" * 32).decode("ascii"),
        "GOVSCOUT_DATABASE": str(tmp_path / "govscout.sqlite3"),
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GOVSCOUT_DEPLOYMENT_MODE", "local"),
        ("GOVSCOUT_BIND_HOST", "0.0.0.0"),
        ("GOVSCOUT_BIND_HOST", "100.72.212.14"),
        ("GOVSCOUT_BIND_HOST", "::1"),
        ("GOVSCOUT_PUBLIC_HOST", "example.com"),
        ("GOVSCOUT_USERNAME", ""),
        ("GOVSCOUT_PASSWORD_HASH", "malformed"),
        ("GOVSCOUT_SESSION_SECRET", "too-short"),
    ],
)
def test_public_factory_fails_closed_on_invalid_environment(tmp_path, key, value):
    environment = _environment(tmp_path)
    environment[key] = value

    with pytest.raises(ValueError):
        create_production_app(environment)


def test_public_factory_migrates_database_and_serves_only_exact_host(tmp_path):
    app = create_production_app(_environment(tmp_path))
    app.testing = True
    client = app.test_client()

    login = client.get("/login", base_url=f"https://{PUBLIC_HOST}")
    forged = client.get(
        "/login",
        base_url=f"https://{PUBLIC_HOST}",
        headers={"X-Forwarded-Host": "attacker.example", "X-Forwarded-For": "203.0.113.8"},
    )
    direct = client.get("/login", base_url="https://127.0.0.1")

    assert login.status_code == 200
    assert forged.status_code == 200
    assert direct.status_code == 400
