from datetime import UTC, datetime
from pathlib import Path

import pytest

from govscout.cli import (
    _default_dependencies,
    _validate_web_host,
    build_locked_web_app,
    build_parser,
    main,
)
from govscout.config import load_settings
from govscout.db import connect_database, migrate
from govscout.sendguard import SendGuard
from govscout.web_hosts import parse_host_header

ROOT = Path(__file__).resolve().parents[1]


def test_sends_today_prints_authoritative_warmup_counter(tmp_path, capsys):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    settings = load_settings(ROOT / "config/default.toml")

    exit_code = main(
        ["sends", "--today"],
        conn=conn,
        guard=SendGuard(settings),
        now=datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == (
        "Drafts today: 0 / 10 soft / 5 effective hard "
        "(configured hard 15; warm-up day 1; 5 remaining)"
    )


def test_default_dependencies_work_without_source_checkout(monkeypatch, tmp_path):
    database = tmp_path / "govscout.sqlite3"
    monkeypatch.setenv("GOVSCOUT_DATABASE", str(database))
    monkeypatch.delenv("GOVSCOUT_CONFIG", raising=False)
    monkeypatch.setattr("govscout.cli.ROOT", tmp_path / "missing-checkout")

    conn, guard = _default_dependencies()

    try:
        assert guard.sender_email == "harrison@misegroup.co.uk"
        assert database.exists()
    finally:
        conn.close()


def test_sends_week_prints_weekly_total_and_authoritative_daily_counter(tmp_path, capsys):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    settings = load_settings(ROOT / "config/default.toml")

    exit_code = main(
        ["sends", "--week"],
        conn=conn,
        guard=SendGuard(settings),
        now=datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Last 7 UK days: 0 countable drafts" in output
    assert "Drafts today: 0 / 10 soft / 5 effective hard" in output


def test_locked_web_runtime_is_local_only_and_requires_no_gmail(monkeypatch, tmp_path):
    database = tmp_path / "govscout.sqlite3"
    monkeypatch.setenv("GOVSCOUT_DATABASE", str(database))
    monkeypatch.delenv("GOVSCOUT_CONFIG", raising=False)

    args = build_parser().parse_args(
        ["web", "--host", "127.0.0.1", "--port", "5050"]
    )
    app = build_locked_web_app()
    response = app.test_client().get("/today")

    assert args.command == "web"
    assert args.host == "127.0.0.1"
    assert args.port == 5050
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "No firms are ready to review yet." in page
    assert "Email drafting" not in page
    assert "LINT_NOT_READY" not in page


def test_web_host_allows_loopback_and_tailscale_only():
    assert _validate_web_host("127.0.0.1") == "127.0.0.1"
    assert _validate_web_host("::1") == "::1"
    assert _validate_web_host("100.64.0.0") == "100.64.0.0"
    assert _validate_web_host("100.127.255.255") == "100.127.255.255"
    assert _validate_web_host("100.72.212.14") == "100.72.212.14"
    assert _validate_web_host("fd7a:115c:a1e0::3601:d4ab") == (
        "fd7a:115c:a1e0::3601:d4ab"
    )
    assert _validate_web_host("fd7a:115c:a1e0:0:0:0:3601:d4ab") == (
        "fd7a:115c:a1e0::3601:d4ab"
    )

    for unsafe in (
        "localhost",
        "0.0.0.0",
        "::",
        "100.63.255.255",
        "100.128.0.0",
        "fd7a:115c:a1df:ffff::1",
        "fd7a:115c:a1e1::1",
        "fd7a:115c:a1e0::1%lo",
        "192.168.1.10",
        "8.8.8.8",
        "example.com",
    ):
        with pytest.raises(SystemExit, match="loopback or Tailscale"):
            _validate_web_host(unsafe)


def test_locked_web_runtime_accepts_only_configured_tailscale_host(
    monkeypatch,
    tmp_path,
):
    database = tmp_path / "govscout.sqlite3"
    monkeypatch.setenv("GOVSCOUT_DATABASE", str(database))
    app = build_locked_web_app(trusted_hosts=("100.72.212.14",))
    app.testing = True

    accepted_ipv4 = app.test_client().get(
        "/today",
        headers={"Host": "100.72.212.14:8766"},
    )
    rejected = app.test_client().get(
        "/today",
        headers={"Host": "attacker.example"},
    )

    assert accepted_ipv4.status_code == 200
    assert rejected.status_code == 400


@pytest.mark.parametrize(
    "host_header",
    [
        "100.72.212.14:bad",
        "100.72.212.14:0",
        "100.72.212.14:8766.evil",
        "[100.72.212.14]",
        "[100.72.212.14]attacker.example",
        "[::1]evil",
        "[fd7a:115c:a1e0::3601:d4ab]:bad",
        "fd7a:115c:a1e0::3601:d4ab",
    ],
)
def test_locked_web_runtime_rejects_malformed_host_headers(
    monkeypatch,
    tmp_path,
    host_header,
):
    monkeypatch.setenv("GOVSCOUT_DATABASE", str(tmp_path / "govscout.sqlite3"))
    app = build_locked_web_app(
        trusted_hosts=("100.72.212.14", "fd7a:115c:a1e0::3601:d4ab")
    )

    response = app.test_client(use_cookies=False).get(
        "/today", headers={"Host": host_header}
    )

    assert response.status_code == 400


def test_raw_host_parser_rejects_out_of_range_ports():
    assert parse_host_header("100.72.212.14:65536") is None
    assert parse_host_header("[fd7a:115c:a1e0::3601:d4ab]:65536") is None


def test_locked_web_runtime_canonicalises_equivalent_ipv6_host(tmp_path, monkeypatch):
    monkeypatch.setenv("GOVSCOUT_DATABASE", str(tmp_path / "govscout.sqlite3"))
    app = build_locked_web_app(trusted_hosts=("fd7a:115c:a1e0::3601:d4ab",))

    response = app.test_client().get(
        "/today",
        headers={"Host": "[fd7a:115c:a1e0:0:0:0:3601:d4ab]:8766"},
    )

    assert response.status_code == 200
