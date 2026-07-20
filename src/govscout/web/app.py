from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import secrets
import sqlite3
from typing import Protocol

from flask import Flask, abort, jsonify, render_template, request, session

from govscout.cli import format_counter
from govscout.draft_service import (
    DraftAlreadySent,
    DraftOutcomeUncertain,
    DraftPolicyRefused,
    DraftService,
)
from govscout.sendguard import (
    ReservationConflict,
    ReservationRequest,
    SendGuard,
    SendLimitExceeded,
)
from govscout.web_hosts import canonical_safe_bind_host, parse_host_header


class CandidateSource(Protocol):
    def get(self, lead_id: int) -> ReservationRequest: ...

    def due(self) -> list[ReservationRequest]: ...


def create_app(
    *,
    conn_factory: Callable[[], sqlite3.Connection],
    guard: SendGuard,
    now_provider: Callable[[], datetime] | None = None,
    draft_service: DraftService | None = None,
    candidate_source: CandidateSource | None = None,
    csrf_secret: bytes | str | None = None,
    trusted_hosts: tuple[str, ...] = (),
) -> Flask:
    app = Flask(__name__)
    app.secret_key = csrf_secret or secrets.token_bytes(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )
    clock = now_provider or (lambda: datetime.now(UTC))
    drafting_locked = draft_service is None or candidate_source is None
    allowed_hosts = {
        "localhost",
        "127.0.0.1",
        "::1",
        *(canonical_safe_bind_host(host) for host in trusted_hosts),
    }

    @app.before_request
    def protect_state_changes():
        host_header = request.environ.get("HTTP_HOST", "")
        hostname = parse_host_header(host_header)
        if hostname not in allowed_hosts:
            abort(400)
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        expected = session.get("csrf_token", "")
        if not supplied or not secrets.compare_digest(supplied, expected):
            abort(403)
        return None

    @app.get("/today")
    def today():
        conn = conn_factory()
        try:
            decision = guard.status(conn, now=clock())
            staged_candidates = conn.execute(
                """
                SELECT id, status, company_name, source_location, source_url
                FROM candidates
                ORDER BY id
                LIMIT 50
                """
            ).fetchall()
        finally:
            conn.close()
        due_candidates = []
        if candidate_source is not None:
            due_candidates = sorted(
                candidate_source.due(),
                key=lambda item: (item.stage == 0, item.lead_id),
            )
        return render_template(
            "today.html",
            counter=format_counter(decision),
            decision=decision,
            drafting_locked=drafting_locked,
            csrf_token=session["csrf_token"],
            due_candidates=due_candidates,
            staged_candidates=staged_candidates,
        )

    @app.post("/today/draft/<int:lead_id>")
    def draft_one(lead_id: int):
        if drafting_locked:
            return jsonify(error="LINT_NOT_READY"), 409
        assert draft_service is not None
        assert candidate_source is not None
        try:
            request = candidate_source.get(lead_id)
        except (KeyError, StopIteration):
            return jsonify(error="lead_not_due"), 404
        conn = conn_factory()
        try:
            result = draft_service.create_review_draft(conn, request, now=clock())
        except DraftPolicyRefused as exc:
            return jsonify(error="lint_refused", reasons=exc.reasons), 422
        except SendLimitExceeded as exc:
            return jsonify(error="daily_limit", remaining=exc.decision.remaining), 429
        except (DraftOutcomeUncertain, DraftAlreadySent, ReservationConflict) as exc:
            return jsonify(error="draft_conflict", detail=str(exc)), 409
        finally:
            conn.close()
        status_code = 201 if result.created else 200
        return (
            jsonify(
                created=result.created,
                draft_id=result.draft_id,
                send_id=result.send_id,
            ),
            status_code,
        )

    @app.post("/today/drafts")
    def draft_batch():
        if drafting_locked:
            return jsonify(error="LINT_NOT_READY"), 409
        assert draft_service is not None
        assert candidate_source is not None
        candidates = sorted(
            candidate_source.due(),
            key=lambda request: (request.stage == 0, request.lead_id),
        )
        drafted = 0
        processed = 0
        conn = conn_factory()
        try:
            for request in candidates:
                try:
                    result = draft_service.create_review_draft(
                        conn,
                        request,
                        now=clock(),
                    )
                except SendLimitExceeded:
                    break
                except DraftPolicyRefused as exc:
                    return jsonify(error="lint_refused", reasons=exc.reasons), 422
                except (
                    DraftOutcomeUncertain,
                    DraftAlreadySent,
                    ReservationConflict,
                ) as exc:
                    return jsonify(error="draft_conflict", detail=str(exc)), 409
                processed += 1
                if result.created:
                    drafted += 1
        finally:
            conn.close()
        return jsonify(drafted=drafted, rolled_over=len(candidates) - processed)

    return app
