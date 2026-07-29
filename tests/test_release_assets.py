from importlib import resources
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_deployment_is_documented_without_secret_values():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "https://leads.misegroup.co.uk" in readme
    assert "browser" in readme.casefold()
    assert "deploy/production/v1/RUNBOOK.md" in readme
    for name in (
        "GOVSCOUT_DEPLOYMENT_MODE",
        "GOVSCOUT_BIND_HOST",
        "GOVSCOUT_PUBLIC_HOST",
        "GOVSCOUT_USERNAME",
        "GOVSCOUT_PASSWORD_HASH",
        "GOVSCOUT_SESSION_SECRET",
    ):
        assert name in env_example
    assert "<versioned-scrypt-hash>" in env_example
    assert "<base64url-encoded-random-secret-at-least-32-bytes>" in env_example


def test_versioned_public_deployment_assets_are_complete():
    release = ROOT / "deploy/production/v1"

    assert "leads.misegroup.co.uk" in (release / "Caddyfile").read_text(encoding="utf-8")
    service = (release / "govscout.service").read_text(encoding="utf-8")
    assert "--bind ${GOVSCOUT_BIND_HOST}:8766" in service
    assert "gunicorn" in service
    runbook = (release / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "88.208.212.58" in runbook
    assert "Rollback" in runbook
    assert "PC browser-only use" in runbook
    assert "global throttle bucket" in runbook
    assert "lock out" in runbook


def test_auth_login_and_latest_migrations_are_package_resources():
    login = (
        resources.files("govscout.web")
        .joinpath("templates", "login.html")
        .read_text(encoding="utf-8")
    )
    migrations = {
        item.name
        for item in resources.files("govscout.resources").joinpath("migrations").iterdir()
        if item.name.endswith(".sql")
    }

    assert "Sign in" in login
    assert "007_login_throttle.sql" in migrations
    assert "008_fca_identity_hardening.sql" in migrations
