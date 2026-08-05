from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import pytest

from govscout.auth import AuthConfig, hash_password
from govscout.config import load_settings
from govscout.db import connect_database, migrate
from govscout.sendguard import SendGuard
from govscout.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_HOST = "leads.misegroup.co.uk"
BASE_URL = f"https://{PUBLIC_HOST}"


def _public_app(tmp_path, *, max_failures=5):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    conn.close()
    auth = AuthConfig(
        username="operator",
        password_hash=hash_password("test-password", salt=b"0123456789abcdef"),
        session_secret=b"s" * 32,
        public_host=PUBLIC_HOST,
        public_https=True,
        max_failures=max_failures,
    )
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        now_provider=lambda: datetime(2026, 7, 29, 9, tzinfo=UTC),
        auth=auth,
    )
    app.testing = True
    return app, database


@pytest.mark.parametrize("username", ["a" * 101, "operator\nname", "opérator"])
def test_authentication_username_matches_audit_actor_contract(username):
    with pytest.raises(ValueError, match="username"):
        AuthConfig(
            username=username,
            password_hash=hash_password(
                "test-password", salt=b"0123456789abcdef"
            ),
            session_secret=b"s" * 32,
        )


def test_public_mode_protects_data_review_and_draft_routes(tmp_path):
    app, database = _public_app(tmp_path)
    client = app.test_client()

    page = client.get("/today", base_url=BASE_URL)
    review = client.post("/today/review/1", base_url=BASE_URL)
    draft = client.post("/today/draft/1", base_url=BASE_URL)

    assert page.status_code == 302
    assert page.headers["Location"] == "/login?next=/today"
    assert review.status_code == 401
    assert draft.status_code == 401
    verify = connect_database(database)
    assert verify.execute("SELECT count(*) FROM firm_reviews").fetchone()[0] == 0
    assert verify.execute("SELECT count(*) FROM sends").fetchone()[0] == 0


def _login(client, *, next_path="/today", password="test-password"):
    login_page = client.get("/login", base_url=BASE_URL)
    with client.session_transaction(base_url=BASE_URL) as browser_session:
        token = browser_session["csrf_token"]
    response = client.post(
        "/login",
        base_url=BASE_URL,
        data={
            "csrf_token": token,
            "username": "operator",
            "password": password,
            "next": next_path,
        },
    )
    return login_page, response


def test_valid_login_rotates_session_and_allows_today_with_secure_cookie(tmp_path):
    app, _database = _public_app(tmp_path)
    client = app.test_client()

    login_page, response = _login(client)

    assert response.status_code == 303
    assert response.headers["Location"] == "/today"
    assert response.headers.getlist("Set-Cookie") != login_page.headers.getlist("Set-Cookie")
    cookie = "; ".join(response.headers.getlist("Set-Cookie"))
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Expires=" in cookie
    assert app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(hours=8)
    with client.session_transaction(base_url=BASE_URL) as browser_session:
        assert browser_session.permanent is True
    page = client.get("/today", base_url=BASE_URL)
    assert page.status_code == 200
    assert "Review possible firms for MISE" in page.get_data(as_text=True)
    assert 'action="/logout"' in page.get_data(as_text=True)


def test_login_from_site_root_goes_to_today(tmp_path):
    app, _database = _public_app(tmp_path)
    client = app.test_client()

    landing = client.get("/", base_url=BASE_URL)
    assert landing.status_code == 302
    assert landing.headers["Location"] == "/login?next=/"

    login_page = client.get(landing.headers["Location"], base_url=BASE_URL)
    with client.session_transaction(base_url=BASE_URL) as browser_session:
        token = browser_session["csrf_token"]
    response = client.post(
        "/login",
        base_url=BASE_URL,
        data={
            "csrf_token": token,
            "username": "operator",
            "password": "test-password",
            "next": "/",
        },
    )

    assert login_page.status_code == 200
    assert response.status_code == 303
    assert response.headers["Location"] == "/today"
    already_authenticated = client.get("/login?next=/", base_url=BASE_URL)
    assert already_authenticated.status_code == 302
    assert already_authenticated.headers["Location"] == "/today"


def test_login_requires_csrf(tmp_path):
    app, _database = _public_app(tmp_path)
    client = app.test_client()

    missing_csrf = client.post(
        "/login",
        base_url=BASE_URL,
        data={"username": "operator", "password": "test-password"},
    )

    assert missing_csrf.status_code == 403


@pytest.mark.parametrize(
    "unsafe_next",
    (
        "//attacker.example/steal",
        "https://attacker.example/steal",
        "/\\attacker.example/steal",
        "\\attacker.example/steal",
    ),
)
def test_login_rejects_unsafe_redirects(tmp_path, unsafe_next):
    app, _database = _public_app(tmp_path)
    client = app.test_client()

    _page, accepted = _login(client, next_path=unsafe_next)

    assert accepted.status_code == 303
    assert accepted.headers["Location"] == "/today"


def test_invalid_login_is_persistently_throttled_without_storing_password(tmp_path):
    app, database = _public_app(tmp_path, max_failures=2)
    first_client = app.test_client()
    _page, first = _login(first_client, password="never-store-this-password")
    second_client = app.test_client()
    _page, second = _login(second_client, password="never-store-this-password")
    restarted_worker = app.test_client()
    login_page = restarted_worker.get("/login", base_url=BASE_URL)
    with restarted_worker.session_transaction(base_url=BASE_URL) as browser_session:
        token = browser_session["csrf_token"]
    blocked = restarted_worker.post(
        "/login",
        base_url=BASE_URL,
        data={"csrf_token": token, "username": "operator", "password": "test-password"},
    )

    assert first.status_code == 401
    assert second.status_code == 429
    assert blocked.status_code == 429
    assert login_page.status_code == 200
    assert b"never-store-this-password" not in database.read_bytes()


def test_concurrent_success_does_not_erase_a_later_failed_login(tmp_path, monkeypatch):
    app, database = _public_app(tmp_path)
    bad_verifying = Event()
    good_finished = Event()
    responses = {}

    def controlled_verify(password, _encoded):
        if password == "bad-password":
            bad_verifying.set()
            assert good_finished.wait(timeout=5)
            return False
        assert bad_verifying.wait(timeout=5)
        return True

    monkeypatch.setattr("govscout.web.app.verify_password", controlled_verify)

    def login_worker(label, password):
        client = app.test_client()
        client.get("/login", base_url=BASE_URL)
        with client.session_transaction(base_url=BASE_URL) as browser_session:
            token = browser_session["csrf_token"]
        responses[label] = client.post(
            "/login",
            base_url=BASE_URL,
            data={
                "csrf_token": token,
                "username": "operator",
                "password": password,
                "next": "/today",
            },
        )
        if label == "good":
            good_finished.set()

    bad = Thread(target=login_worker, args=("bad", "bad-password"))
    good = Thread(target=login_worker, args=("good", "test-password"))
    bad.start()
    good.start()
    bad.join()
    good.join()

    assert responses["good"].status_code == 303
    assert responses["bad"].status_code == 401
    verify = connect_database(database)
    row = verify.execute("SELECT failure_count FROM login_throttle").fetchone()
    assert row[0] == 1


def test_declared_oversized_requests_are_rejected_before_route_or_auth_returns(tmp_path):
    app, _database = _public_app(tmp_path)
    client = app.test_client()

    oversized_login_get = client.get(
        "/login",
        base_url=BASE_URL,
        environ_overrides={"CONTENT_LENGTH": "16385"},
    )
    oversized_unauthenticated_post = client.post(
        "/today/draft/1",
        base_url=BASE_URL,
        data=b"x" * 16_385,
        content_type="application/octet-stream",
    )

    assert oversized_login_get.status_code == 413
    assert oversized_unauthenticated_post.status_code == 413


@pytest.mark.parametrize("declared_length", ["not-a-number", "-1"])
def test_malformed_or_negative_declared_request_lengths_fail_closed(tmp_path, declared_length):
    app, _database = _public_app(tmp_path)
    client = app.test_client()

    response = client.get(
        "/login",
        base_url=BASE_URL,
        environ_overrides={"CONTENT_LENGTH": declared_length},
    )

    assert response.status_code == 413


def test_forged_host_is_rejected_before_login_and_security_headers_are_set(tmp_path):
    app, _database = _public_app(tmp_path)
    client = app.test_client()

    forged = client.get("/login", headers={"Host": "attacker.example"})
    login = client.get("/login", base_url=BASE_URL)

    assert forged.status_code == 400
    assert login.status_code == 200
    assert login.headers["X-Frame-Options"] == "DENY"
    assert login.headers["X-Content-Type-Options"] == "nosniff"
    assert login.headers["Referrer-Policy"] == "no-referrer"
    assert login.headers["Cache-Control"] == "no-store"
    policy = login.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in policy
    nonce_match = re.search(r"style-src 'nonce-([^']+)'", policy)
    assert nonce_match is not None
    assert f'<style nonce="{nonce_match.group(1)}">' in login.get_data(as_text=True)
    assert "'unsafe-inline'" not in policy
    assert login.headers["Strict-Transport-Security"] == "max-age=31536000"

    oversized = client.post(
        "/login",
        base_url=BASE_URL,
        data={"username": "operator", "password": "x" * 20_000, "csrf_token": "x"},
    )
    assert oversized.status_code == 413


def test_logout_requires_csrf_clears_session_and_cookie(tmp_path):
    app, _database = _public_app(tmp_path)
    client = app.test_client()
    _page, _response = _login(client)

    refused = client.post("/logout", base_url=BASE_URL)
    with client.session_transaction(base_url=BASE_URL) as browser_session:
        token = browser_session["csrf_token"]
    logged_out = client.post(
        "/logout",
        base_url=BASE_URL,
        data={"csrf_token": token},
    )

    assert refused.status_code == 403
    assert logged_out.status_code == 303
    assert logged_out.headers["Location"] == "/login"
    assert "Expires=Thu, 01 Jan 1970" in logged_out.headers["Set-Cookie"]
    assert client.get("/today", base_url=BASE_URL).status_code == 302
