from __future__ import annotations

import hashlib
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask import (
    Request as FlaskRequest,
)

from govscout.auth import (
    AuthConfig,
    LoginThrottle,
    admit_collector_request,
    authenticate_collector_token,
    verify_password,
)
from govscout.collector_imports import COLLECTOR_BATCH_LIMIT, process_collector_import
from govscout.contact_research import record_contact_evidence
from govscout.draft_service import (
    DraftAlreadySent,
    DraftOutcomeUncertain,
    DraftPolicyRefused,
    DraftService,
)
from govscout.fca_discovery import (
    FCA_MAX_RESPONSE_BYTES,
    FcaDataError,
    fca_register_search_url,
    parse_fca_json,
)
from govscout.quality import qc_is_current, review_firm
from govscout.research import ResearchConflict, record_archive_event
from govscout.website_research import (
    WebsiteResearchConflict,
    confirm_website_and_enqueue,
    enqueue_website_reprocessing,
    record_website_evidence,
)
from govscout.website_candidates import (
    WebsiteCandidateProvider,
    candidate_urls_are_safe,
    discover_website_candidates,
    load_confirmable_candidate,
)
from govscout.sendguard import (
    ReservationConflict,
    ReservationRequest,
    SendGuard,
    SendLimitExceeded,
)
from govscout.web_hosts import canonical_safe_bind_host, parse_host_header

COLLECTOR_IMPORT_PATH = "/api/v1/collector/imports"
COLLECTOR_IMPORT_LIMIT_PER_DEVICE = 100


class GovScoutRequest(FlaskRequest):
    @property
    def max_content_length(self) -> int | None:
        explicit_limit = getattr(self, "_max_content_length", None)
        if explicit_limit is not None:
            return explicit_limit
        if self.path == COLLECTOR_IMPORT_PATH:
            return FCA_MAX_RESPONSE_BYTES
        return super().max_content_length

    @max_content_length.setter
    def max_content_length(self, value: int | None) -> None:
        self._max_content_length = value


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
    auth: AuthConfig | None = None,
    website_candidate_provider: WebsiteCandidateProvider | None = None,
) -> Flask:
    app = Flask(__name__)
    app.request_class = GovScoutRequest
    app.secret_key = auth.session_secret if auth else (csrf_secret or secrets.token_bytes(32))
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=auth is not None,
        SESSION_COOKIE_NAME="govscout_session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=16_384,
    )
    clock = now_provider or (lambda: datetime.now(UTC))
    drafting_locked = draft_service is None or candidate_source is None
    if auth is None:
        allowed_hosts = {
            "localhost",
            "127.0.0.1",
            "::1",
            *(canonical_safe_bind_host(host) for host in trusted_hosts),
        }
    else:
        allowed_hosts = {auth.public_host}
    throttle = LoginThrottle(max_failures=auth.max_failures) if auth else None

    def safe_next(value: str | None) -> str:
        if value == "/":
            return url_for("today")
        if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
            return value
        return url_for("today")

    @app.before_request
    def protect_request():
        g.csp_nonce = secrets.token_urlsafe(24)
        raw_content_length = request.environ.get("CONTENT_LENGTH")
        if raw_content_length not in (None, ""):
            if (
                not isinstance(raw_content_length, str)
                or not raw_content_length.isascii()
                or not raw_content_length.isdecimal()
            ):
                abort(413)
            declared_content_length = int(raw_content_length)
            if (
                request.max_content_length is not None
                and declared_content_length > request.max_content_length
            ):
                abort(413)
        host_header = request.environ.get("HTTP_HOST", "")
        hostname = parse_host_header(host_header)
        if hostname not in allowed_hosts:
            abort(400)

        if request.endpoint == "collector_import":
            return None

        auth_exempt = request.endpoint in {"login", "static"}
        if auth is not None and not auth_exempt and session.get("authenticated") is not True:
            if request.method in {"GET", "HEAD"}:
                return redirect(url_for("login", next=request.path))
            abort(401)

        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        expected = session.get("csrf_token", "")
        if not supplied or not secrets.compare_digest(supplied, expected):
            abort(403)
        return None

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; form-action 'self'; "
            f"frame-ancestors 'none'; object-src 'none'; style-src 'nonce-{g.csp_nonce}'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if auth is not None and auth.public_https:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.context_processor
    def inject_csp_nonce():
        return {"csp_nonce": g.csp_nonce}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if auth is None:
            abort(404)
        if request.method == "GET":
            if session.get("authenticated") is True:
                return redirect(safe_next(request.args.get("next")))
            return render_template(
                "login.html",
                csrf_token=session["csrf_token"],
                next_path=safe_next(request.args.get("next")),
                error=None,
            )

        conn = conn_factory()
        try:
            assert throttle is not None
            admission = throttle.reserve_attempt(conn, now=clock())
            if not admission.admitted:
                return render_template(
                    "login.html",
                    csrf_token=session["csrf_token"],
                    next_path=safe_next(request.form.get("next")),
                    error="Too many attempts. Try again later.",
                ), 429
            assert admission.token is not None
            supplied_username = request.form.get("username", "")
            supplied_password = request.form.get("password", "")
            valid_username = secrets.compare_digest(supplied_username, auth.username)
            valid_password = verify_password(supplied_password, auth.password_hash)
            if not (valid_username and valid_password):
                blocked_after = throttle.finalize_failure(
                    conn,
                    token=admission.token,
                    now=clock(),
                )
                status = 429 if blocked_after else 401
                error = (
                    "Too many attempts. Try again later."
                    if blocked_after
                    else "Invalid credentials."
                )
                return render_template(
                    "login.html",
                    csrf_token=session["csrf_token"],
                    next_path=safe_next(request.form.get("next")),
                    error=error,
                ), status
            throttle.finalize_success(conn, token=admission.token)
        finally:
            conn.close()
        destination = safe_next(request.form.get("next"))
        session.clear()
        session.permanent = True
        session["authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        return redirect(destination, code=303)

    @app.post("/logout")
    def logout():
        session.clear()
        response = redirect(url_for("login"), code=303)
        response.delete_cookie(
            app.config["SESSION_COOKIE_NAME"],
            secure=bool(auth),
            httponly=True,
            samesite="Strict",
        )
        return response

    @app.post("/api/v1/collector/imports")
    def collector_import():
        if auth is None:
            abort(404)
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or authorization.count(" ") != 1:
            return jsonify(error="collector_unauthorized"), 401
        token = authorization.removeprefix("Bearer ")
        conn = conn_factory()
        try:
            conn.execute("BEGIN IMMEDIATE")
            device_id = authenticate_collector_token(conn, token, now=clock())
            if device_id is None:
                conn.execute("ROLLBACK")
                return jsonify(error="collector_unauthorized"), 401
            if not admit_collector_request(conn, device_id=device_id, now=clock()):
                conn.execute("COMMIT")
                return jsonify(error="collector_rate_limited"), 429
            conn.execute("COMMIT")
            import_id = request.headers.get("Idempotency-Key", "")
            claimed_hash = request.headers.get("X-Payload-SHA256", "")
            if (
                len(import_id) != 32
                or any(character not in "0123456789abcdef" for character in import_id)
                or len(claimed_hash) != 64
                or any(character not in "0123456789abcdef" for character in claimed_hash)
            ):
                return jsonify(error="invalid_import_identity"), 422
            if request.mimetype != "application/json":
                return jsonify(error="content_type_required"), 415
            payload = request.get_data(cache=False)
            actual_hash = hashlib.sha256(payload).hexdigest()
            if not secrets.compare_digest(actual_hash, claimed_hash):
                return jsonify(error="payload_hash_mismatch"), 422
            try:
                records = parse_fca_json(payload)
                payload_text = payload.decode("utf-8")
            except (FcaDataError, UnicodeDecodeError) as exc:
                return jsonify(error="invalid_fca_export", detail=str(exc)), 422
            if len(records) > COLLECTOR_BATCH_LIMIT:
                return jsonify(error="collector_batch_limit_exceeded"), 422

            conn.execute("BEGIN IMMEDIATE")
            confirmed_device_id = authenticate_collector_token(conn, token, now=clock())
            if confirmed_device_id != device_id:
                conn.execute("ROLLBACK")
                return jsonify(error="collector_unauthorized"), 401
            existing = conn.execute(
                """
                SELECT import_id, device_id, payload_sha256, state
                FROM collector_imports
                WHERE import_id = ?
                """,
                (import_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["device_id"] != device_id
                    or existing["payload_sha256"] != actual_hash
                ):
                    conn.execute("ROLLBACK")
                    return jsonify(error="collector_import_conflict"), 409
                conn.execute("COMMIT")
                state = existing["state"]
                if state == "pending":
                    state = process_collector_import(
                        conn,
                        import_id=existing["import_id"],
                        now=clock(),
                    ).state
                return jsonify(
                    import_id=existing["import_id"],
                    payload_sha256=existing["payload_sha256"],
                    state=state,
                ), 200
            import_count = conn.execute(
                "SELECT count(*) FROM collector_imports WHERE device_id = ?",
                (device_id,),
            ).fetchone()[0]
            if import_count >= COLLECTOR_IMPORT_LIMIT_PER_DEVICE:
                conn.execute("ROLLBACK")
                return jsonify(error="collector_storage_limit"), 429
            conn.execute(
                """
                INSERT INTO collector_imports (
                    import_id, device_id, payload_sha256, payload_json,
                    state, received_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (
                    import_id,
                    device_id,
                    actual_hash,
                    payload_text,
                    clock().astimezone(UTC).isoformat(),
                ),
            )
            conn.execute("COMMIT")
            state = process_collector_import(
                conn,
                import_id=import_id,
                now=clock(),
            ).state
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return jsonify(
            import_id=import_id,
            payload_sha256=actual_hash,
            state=state,
        ), 202

    @app.get("/today")
    def today():
        current = clock()
        conn = conn_factory()
        try:
            firm_rows = conn.execute(
                """
                SELECT f.*,
                    e.id AS enrichment_run_id, e.state AS enrichment_state,
                    e.score, e.temperature, e.completed_at AS enriched_at,
                    q.id AS qc_run_id, q.state AS qc_state,
                    q.reason_codes AS qc_reasons, q.expires_at AS qc_expires_at,
                    r.decision AS review_decision, r.notes AS review_notes,
                    r.rejection_reason,
                    t.id AS archive_event_id,
                    t.action AS archive_action,
                    t.reason AS archive_reason,
                    w.id AS website_evidence_event_id,
                    w.action AS website_evidence_action,
                    w.website_url AS researched_website_url,
                    w.evidence_url AS website_evidence_url,
                    w.justification AS website_justification,
                    c.id AS contact_evidence_event_id,
                    c.action AS contact_evidence_action,
                    c.email AS researched_email,
                    c.phone AS researched_phone,
                    c.contact_name AS researched_contact_name,
                    c.evidence_url AS contact_evidence_url,
                    c.justification AS contact_justification,
                    p.id AS reprocessing_job_id,
                    p.state AS reprocessing_state,
                    p.outcome_code AS reprocessing_outcome
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
                LEFT JOIN firm_archive_events t ON t.id = (
                    SELECT id FROM firm_archive_events
                    WHERE firm_id = f.id ORDER BY id DESC LIMIT 1
                )
                LEFT JOIN firm_website_evidence_events w ON w.id = (
                    SELECT id FROM firm_website_evidence_events
                    WHERE firm_id = f.id ORDER BY id DESC LIMIT 1
                )
                LEFT JOIN firm_contact_evidence_events c ON c.id = (
                    SELECT id FROM firm_contact_evidence_events
                    WHERE firm_id = f.id ORDER BY id DESC LIMIT 1
                )
                LEFT JOIN fca_reprocessing_jobs p ON p.id = (
                    SELECT id FROM fca_reprocessing_jobs
                    WHERE firm_id = f.id ORDER BY id DESC LIMIT 1
                )
                ORDER BY COALESCE(e.score, -1) DESC, f.id
                """
            ).fetchall()
            research_firms = []
            review_firms = []
            archived_firms = []
            for row in firm_rows:
                archived = row["archive_action"] == "archive"
                qc_current = bool(
                    not archived
                    and row["qc_run_id"] is not None
                    and qc_is_current(
                        conn,
                        firm_id=row["id"],
                        qc_run_id=row["qc_run_id"],
                        now=current,
                    )
                )
                if archived:
                    target = archived_firms
                else:
                    target = review_firms if qc_current else research_firms
                if len(target) >= 50:
                    continue
                item = dict(row)
                item["qc_current"] = qc_current
                item["fca_register_url"] = fca_register_search_url(row["frn"])
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
                item["verification_attempts"] = [
                    dict(attempt)
                    for attempt in conn.execute(
                        """
                        SELECT state, reason_code, checked_at, company_number,
                               legal_name, legal_form, company_status
                        FROM company_verification_attempts
                        WHERE firm_id = ? ORDER BY id DESC LIMIT 10
                        """,
                        (row["id"],),
                    ).fetchall()
                ]
                latest_candidate_search = conn.execute(
                    """
                    SELECT id FROM website_candidate_searches
                    WHERE firm_id = ? ORDER BY id DESC LIMIT 1
                    """,
                    (row["id"],),
                ).fetchone()
                item["website_candidates"] = (
                    []
                    if latest_candidate_search is None
                    else [
                        dict(candidate)
                        for candidate in conn.execute(
                            """
                            SELECT id, rank, website_url, source_url, title, snippet
                            FROM website_candidates
                            WHERE search_id = ? ORDER BY rank
                            """,
                            (latest_candidate_search["id"],),
                        ).fetchall()
                        if candidate_urls_are_safe(
                            website_url=candidate["website_url"],
                            source_url=candidate["source_url"],
                        )
                    ]
                )
                target.append(item)
                if (
                    len(research_firms) >= 50
                    and len(review_firms) >= 50
                    and len(archived_firms) >= 50
                ):
                    break
        finally:
            conn.close()
        return render_template(
            "today.html",
            csrf_token=session["csrf_token"],
            research_firms=research_firms,
            review_firms=review_firms,
            archived_firms=archived_firms,
            candidate_search_enabled=website_candidate_provider is not None,
            auth_enabled=auth is not None,
        )

    @app.post("/today/research/<int:firm_id>/archive")
    def archive_research_firm(firm_id: int):
        raw_expected = request.form.get("expected_archive_event_id")
        try:
            expected_event_id = int(raw_expected) if raw_expected else None
        except ValueError:
            return jsonify(error="invalid_archive_event"), 422
        conn = conn_factory()
        try:
            record_archive_event(
                conn,
                firm_id=firm_id,
                action=request.form.get("action", ""),
                reason=request.form.get("reason"),
                actor=auth.username if auth is not None else "local-operator",
                expected_previous_event_id=expected_event_id,
                now=clock(),
            )
        except ResearchConflict as exc:
            return jsonify(error="archive_conflict", detail=str(exc)), 409
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            return jsonify(error="archive_refused", detail=str(exc)), 422
        finally:
            conn.close()
        return redirect(url_for("today"), code=303)

    @app.post("/today/research/<int:firm_id>/website")
    def record_researched_website(firm_id: int):
        raw_expected = request.form.get("expected_website_evidence_event_id")
        try:
            expected_event_id = int(raw_expected) if raw_expected else None
        except ValueError:
            return jsonify(error="invalid_website_evidence_event"), 422
        conn = conn_factory()
        try:
            record_website_evidence(
                conn,
                firm_id=firm_id,
                action=request.form.get("action", "assert"),
                website_url=request.form.get("website_url"),
                evidence_url=request.form.get("evidence_url"),
                justification=request.form.get("justification"),
                actor=auth.username if auth is not None else "local-operator",
                expected_previous_event_id=expected_event_id,
                now=clock(),
            )
        except WebsiteResearchConflict as exc:
            return jsonify(error="website_evidence_conflict", detail=str(exc)), 409
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            return jsonify(error="website_evidence_refused", detail=str(exc)), 422
        finally:
            conn.close()
        return redirect(url_for("today"), code=303)

    @app.post("/today/research/<int:firm_id>/contact")
    def record_researched_contact(firm_id: int):
        raw_expected = request.form.get("expected_contact_evidence_event_id")
        try:
            expected_event_id = int(raw_expected) if raw_expected else None
        except ValueError:
            return jsonify(error="invalid_contact_evidence_event"), 422
        conn = conn_factory()
        try:
            record_contact_evidence(
                conn,
                firm_id=firm_id,
                action=request.form.get("action", "assert"),
                email=request.form.get("email"),
                phone=request.form.get("phone"),
                contact_name=request.form.get("contact_name"),
                evidence_url=request.form.get("evidence_url"),
                justification=request.form.get("justification"),
                actor=auth.username if auth is not None else "local-operator",
                expected_previous_event_id=expected_event_id,
                now=clock(),
            )
        except WebsiteResearchConflict as exc:
            return jsonify(error="contact_evidence_conflict", detail=str(exc)), 409
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            return jsonify(error="contact_evidence_refused", detail=str(exc)), 422
        finally:
            conn.close()
        return redirect(url_for("today"), code=303)

    @app.post("/today/research/<int:firm_id>/website/confirm")
    def confirm_researched_website(firm_id: int):
        raw_expected = request.form.get("expected_website_evidence_event_id")
        try:
            expected_event_id = int(raw_expected) if raw_expected else None
        except ValueError:
            return jsonify(error="invalid_website_evidence_event"), 422
        conn = conn_factory()
        try:
            confirm_website_and_enqueue(
                conn,
                firm_id=firm_id,
                website_url=request.form.get("website_url"),
                evidence_url=request.form.get("evidence_url"),
                justification=request.form.get("justification"),
                actor=auth.username if auth is not None else "local-operator",
                expected_previous_event_id=expected_event_id,
                request_reason="Confirmed official website through the research workbench.",
                now=clock(),
            )
        except WebsiteResearchConflict as exc:
            return jsonify(error="website_confirmation_conflict", detail=str(exc)), 409
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            return jsonify(error="website_confirmation_refused", detail=str(exc)), 422
        finally:
            conn.close()
        return redirect(url_for("today"), code=303)

    @app.post("/today/research/<int:firm_id>/website/candidates")
    def find_website_candidates(firm_id: int):
        if website_candidate_provider is None:
            return jsonify(error="website_candidate_search_unavailable"), 503
        conn = conn_factory()
        try:
            discover_website_candidates(
                conn,
                firm_id=firm_id,
                provider=website_candidate_provider,
                now=clock(),
            )
        except WebsiteResearchConflict as exc:
            return jsonify(error="website_candidate_search_refused", detail=str(exc)), 409
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            return jsonify(error="website_candidate_search_invalid", detail=str(exc)), 422
        finally:
            conn.close()
        return redirect(url_for("today"), code=303)

    @app.post("/today/research/<int:firm_id>/website/candidates/<int:candidate_id>/confirm")
    def confirm_website_candidate(firm_id: int, candidate_id: int):
        raw_expected = request.form.get("expected_website_evidence_event_id", "")
        try:
            expected_previous = int(raw_expected) if raw_expected else None
        except ValueError:
            return jsonify(error="invalid_website_evidence_event_id"), 422
        conn = conn_factory()
        try:
            candidate = load_confirmable_candidate(
                conn, firm_id=firm_id, candidate_id=candidate_id
            )
            confirm_website_and_enqueue(
                conn,
                firm_id=firm_id,
                website_url=candidate["website_url"],
                evidence_url=candidate["source_url"],
                justification="Operator confirmed this candidate as the firm's official website.",
                actor=auth.username if auth is not None else "local-operator",
                expected_previous_event_id=expected_previous,
                request_reason="Confirmed suggested website through the research workbench.",
                now=clock(),
            )
        except WebsiteResearchConflict as exc:
            return jsonify(error="website_candidate_conflict", detail=str(exc)), 409
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            return jsonify(error="website_candidate_refused", detail=str(exc)), 422
        finally:
            conn.close()
        return redirect(url_for("today"), code=303)

    @app.post("/today/research/<int:firm_id>/website/reprocess")
    def request_website_reprocessing(firm_id: int):
        raw_evidence = request.form.get("website_evidence_event_id")
        try:
            evidence_event_id = int(raw_evidence) if raw_evidence else None
        except ValueError:
            return jsonify(error="invalid_website_evidence_event"), 422
        if evidence_event_id is None:
            return jsonify(error="website_evidence_required"), 422
        conn = conn_factory()
        try:
            enqueue_website_reprocessing(
                conn,
                firm_id=firm_id,
                expected_website_evidence_event_id=evidence_event_id,
                requested_by=auth.username if auth is not None else "local-operator",
                request_reason=request.form.get("request_reason"),
                now=clock(),
            )
        except WebsiteResearchConflict as exc:
            return jsonify(error="reprocessing_conflict", detail=str(exc)), 409
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            return jsonify(error="reprocessing_refused", detail=str(exc)), 422
        finally:
            conn.close()
        return redirect(url_for("today"), code=303)

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
