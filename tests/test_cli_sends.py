from datetime import UTC, datetime
from pathlib import Path

from govscout.cli import _default_dependencies, build_locked_web_app, build_parser, main
from govscout.config import load_settings
from govscout.db import connect_database, migrate
from govscout.sendguard import SendGuard


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
        assert guard.settings.sender_email == "harrison@misegroup.co.uk"
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

    args = build_parser().parse_args(["web", "--port", "5050"])
    app = build_locked_web_app()
    response = app.test_client().get("/today")

    assert args.command == "web"
    assert args.port == 5050
    assert response.status_code == 200
    assert "Production drafting locked: LINT_NOT_READY" in response.get_data(as_text=True)
