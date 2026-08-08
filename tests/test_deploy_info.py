import subprocess

import pytest

from govscout.web.deploy_info import read_deployed_commit


def test_reads_commit_from_release_file(tmp_path):
    (tmp_path / "RELEASE").write_text(
        "commit=abcdef1234567890\nbuilt_at=2026-08-08T09:00:00Z\n", encoding="utf-8"
    )

    assert read_deployed_commit(release_dir=tmp_path) == "abcdef1"


def test_falls_back_to_git_when_no_release_file(tmp_path, monkeypatch):
    def fake_run(args, **kwargs):
        assert args[:2] == ["git", "rev-parse"]
        return subprocess.CompletedProcess(args, 0, stdout="1234567\n", stderr="")

    monkeypatch.setattr("govscout.web.deploy_info.subprocess.run", fake_run)

    assert read_deployed_commit(release_dir=tmp_path) == "1234567"


def test_falls_back_to_unknown_when_git_unavailable(tmp_path, monkeypatch):
    def fake_run(args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("govscout.web.deploy_info.subprocess.run", fake_run)

    assert read_deployed_commit(release_dir=tmp_path) == "unknown"


def test_falls_back_to_unknown_when_git_fails(tmp_path, monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.CalledProcessError(128, args)

    monkeypatch.setattr("govscout.web.deploy_info.subprocess.run", fake_run)

    assert read_deployed_commit(release_dir=tmp_path) == "unknown"


def test_malformed_release_file_falls_through_to_git(tmp_path, monkeypatch):
    (tmp_path / "RELEASE").write_text("built_at=2026-08-08T09:00:00Z\n", encoding="utf-8")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="fedcba9\n", stderr="")

    monkeypatch.setattr("govscout.web.deploy_info.subprocess.run", fake_run)

    assert read_deployed_commit(release_dir=tmp_path) == "fedcba9"


def test_empty_release_dir_and_no_git_returns_unknown(tmp_path, monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 5)

    monkeypatch.setattr("govscout.web.deploy_info.subprocess.run", fake_run)

    assert read_deployed_commit(release_dir=tmp_path) == "unknown"


def test_today_page_renders_deployed_commit_footer(tmp_path):
    from govscout.config import load_settings
    from govscout.db import connect_database, migrate
    from govscout.sendguard import SendGuard
    from govscout.web.app import create_app
    from pathlib import Path

    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    conn.close()
    root = Path(__file__).resolve().parents[1]
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(load_settings(root / "config/default.toml")),
    )

    page = app.test_client().get("/today").get_data(as_text=True)

    assert "Deployed " in page
    assert "class=\"deploy-footer\"" in page
