from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import secrets
import sqlite3
from typing import Protocol

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

from govscout.cli import format_counter
from govscout.draft_service import (
    DraftAlreadySent,
    DraftOutcomeUncertain,
    DraftPolicyRefused,
    DraftService,
)
from govscout.quality import review_firm
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
            firm_rows = conn.execute(
                """
                SELECT f.*,
                    e.id AS enrichment_run_id, e.state AS enrichment_state,
                    e.score, e.temperature, e.completed_at AS enriched_at,
                    q.id AS qc_run_id, q.state AS qc_state,
                    q.reason_codes AS qc_reasons, q.expires_at AS qc_expires_at,
                    r.decision AS review_decision, r.notes AS review_notes,
                    r.rejection_reason
                FROM fca_firms f
                LEFT JOIN enrichment_runs e ON e.id = (
                    SELECT id FROM enrichment_runs
                    WHERE firm_id = f.id ORDER BY id DESC LIMIT 1
                )
                LEFT JOIN qc_runs q ON q.id = (
                    SELECT id FROM qc_runs
                    WHERE firm_id = f.id ORDER BY id DESC LIMIT 1
                )
                LEFT JOIN firm_reviews r ON r.id = (
                    SELECT id FROM firm_reviews
                    WHERE firm_id = f.id ORDER BY id DESC LIMIT 1
                )
                ORDER BY COALESCE(e.score, -1) DESC, f.id
                LIMIT 50
                """
            ).fetchall()
            fca_firms = []
            for row in firm_rows:
                item = dict(row)
                if row["enrichment_run_id"] is None:
                    item["evidence"] = []
                else:
                    item["evidence"] = [
                        dict(evidence)
                        for evidence in conn.execute(
                            """
                            SELECT signal_group, code, evidence_state, source_url, excerpt
                            FROM evidence_items
                            WHERE run_id = ? ORDER BY signal_group, code
                            """,
                            (row["enrichment_run_id"],),
                        ).fetchall()
                    ]
                fca_firms.append(item)
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
            fca_firms=fca_firms,
        )

    @app.post("/today/review/<int:firm_id>")
    def review_one(firm_id: int):
        decision_value = request.form.get("decision", "")
        raw_qc_run_id = request.form.get("qc_run_id")
        try:
            qc_run_id = int(raw_qc_run_id) if raw_qc_run_id else None
        except ValueError:
            return jsonify(error="invalid_qc_run"), 422
        conn = conn_factory()
        try:
            review_firm(
                conn,
                firm_id=firm_id,
                decision=decision_value,
                qc_run_id=qc_run_id,
                notes=request.form.get("notes"),
                rejection_reason=request.form.get("rejection_reason"),
                now=clock(),
            )
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            return jsonify(error="review_refused", detail=str(exc)), 422
        finally:
            conn.close()
        return redirect(url_for("today"), code=303)

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
