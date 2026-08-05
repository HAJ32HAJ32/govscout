from __future__ import annotations

import argparse
import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from govscout.auth import create_collector_device, revoke_collector_device
from govscout.collector_imports import enqueue_historical_collector_imports
from govscout.companies_house import CompaniesHouseClient
from govscout.companies_house_http import (
    CompaniesHouseHttpTransport,
    CompaniesHouseTransportError,
)
from govscout.config import Settings, load_default_settings, load_settings
from govscout.db import connect_database, migrate
from govscout.draft_service import (
    DraftAlreadySent,
    DraftOutcomeUncertain,
    DraftPolicyRefused,
    DraftService,
)
from govscout.enrichment import SiteFetchError, SiteTransport, UrlSiteTransport, run_enrichment
from govscout.fca_discovery import (
    FCA_MAX_RESPONSE_BYTES,
    FcaDataError,
    ingest_fca_records,
    parse_fca_json,
)
from govscout.fca_pipeline import (
    CompanyVerifier,
    FcaEligibilityError,
    verify_and_promote_firm,
    verify_firm,
)
from govscout.processing import process_firm
from govscout.processing_queue import run_pending_jobs
from govscout.quality import run_qc
from govscout.retirement import create_verified_backup, retire_lca_candidates
from govscout.sendguard import (
    GuardDecision,
    ReservationConflict,
    ReservationRequest,
    SendGuard,
    SendLimitExceeded,
)
from govscout.web_hosts import canonical_safe_bind_host

ROOT = Path(__file__).resolve().parents[2]


class CandidateSource(Protocol):
    def get(self, lead_id: int) -> ReservationRequest: ...

    def due(self) -> list[ReservationRequest]: ...


def _validate_web_host(host: str) -> str:
    try:
        return canonical_safe_bind_host(host)
    except ValueError as exc:
        raise SystemExit(
            "web host must be a loopback or Tailscale IP literal"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="govscout")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sends = subparsers.add_parser("sends", help="show the sends ledger counter")
    period = sends.add_mutually_exclusive_group()
    period.add_argument("--today", action="store_true", help="show today's count")
    period.add_argument("--week", action="store_true", help="show the last seven UK days")
    draft = subparsers.add_parser("draft", help="create one review draft")
    draft.add_argument("lead_id", type=int)
    subparsers.add_parser("draft-batch", help="create due review drafts within capacity")
    undo = subparsers.add_parser("send-undo", help="delete a Gmail draft and void its ledger row")
    undo.add_argument("send_id", type=int)
    web = subparsers.add_parser("web", help="run the locked private review interface")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=5000)
    ingest_fca = subparsers.add_parser(
        "ingest-fca", help="stage a bounded FCA Register export"
    )
    ingest_fca.add_argument("--input", required=True, type=Path)
    ingest_fca.add_argument("--limit", type=int, default=25)
    fca_firms = subparsers.add_parser(
        "fca-firms", help="list FCA firms and latest score"
    )
    fca_firms.add_argument("--limit", type=int, default=25)
    enrich_fca = subparsers.add_parser(
        "enrich-fca", help="scan one FCA firm's public website"
    )
    enrich_fca.add_argument("firm_id", type=int)
    qc_fca = subparsers.add_parser(
        "qc-fca", help="run fail-closed quality checks for one FCA firm"
    )
    qc_fca.add_argument("firm_id", type=int)
    verify_fca = subparsers.add_parser(
        "verify-fca", help="verify one FCA firm against Companies House"
    )
    verify_fca.add_argument("firm_id", type=int)
    reverify_fca = subparsers.add_parser(
        "reverify-fca", help="append a fresh Companies House verification"
    )
    reverify_fca.add_argument("firm_id", type=int)
    process_fca = subparsers.add_parser(
        "process-fca", help="verify, enrich, and quality-check one FCA firm"
    )
    process_fca.add_argument("firm_id", type=int)
    process_queue = subparsers.add_parser(
        "process-fca-queue", help="process bounded due FCA jobs"
    )
    process_queue.add_argument("--limit", type=int, default=10)
    enqueue_history = subparsers.add_parser(
        "enqueue-fca-history",
        help="enqueue missing jobs from accepted historical Collector imports",
    )
    enqueue_history.add_argument("--limit", type=int, default=25)
    enqueue_history.add_argument("--dry-run", action="store_true")
    promote_contact = subparsers.add_parser(
        "promote-fca-contact", help="attach a verified outreach contact to an FCA firm"
    )
    promote_contact.add_argument("firm_id", type=int)
    promote_contact.add_argument("--contact-email", required=True)
    collector_add = subparsers.add_parser(
        "collector-device-add", help="create a scoped collector upload credential"
    )
    collector_add.add_argument("--name", required=True)
    collector_revoke = subparsers.add_parser(
        "collector-device-revoke", help="revoke a collector upload credential"
    )
    collector_revoke.add_argument("device_id")
    retire_lca = subparsers.add_parser(
        "retire-lca", help="retire legacy LCA candidates after a verified backup"
    )
    retire_lca.add_argument("--backup", required=True, type=Path)
    return parser


def format_counter(decision: GuardDecision) -> str:
    return (
        f"Drafts today: {decision.today_count} / {decision.soft_limit} soft / "
        f"{decision.effective_hard_limit} effective hard "
        f"(configured hard {decision.configured_hard_limit}; "
        f"warm-up day {decision.warmup_day}; {decision.remaining} remaining)"
    )


def _default_settings() -> Settings:
    config_override = os.environ.get("GOVSCOUT_CONFIG")
    return load_settings(config_override) if config_override else load_default_settings()


def _default_database_path() -> Path:
    data_home = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    )
    return Path(
        os.environ.get(
            "GOVSCOUT_DATABASE", data_home / "govscout" / "govscout.sqlite3"
        )
    )


def _default_company_verifier() -> CompanyVerifier:
    api_key = os.environ.get("GOVSCOUT_COMPANIES_HOUSE_API_KEY", "")
    if not api_key:
        raise ValueError("GOVSCOUT_COMPANIES_HOUSE_API_KEY is not configured")
    return CompaniesHouseClient(CompaniesHouseHttpTransport(api_key=api_key))


def _default_dependencies() -> tuple[sqlite3.Connection, SendGuard]:
    settings = _default_settings()
    conn = connect_database(_default_database_path())
    migrate(conn)
    return conn, SendGuard(settings)


def build_locked_web_app(*, trusted_hosts: tuple[str, ...] = ()):
    from govscout.web.app import create_app

    database = _default_database_path()
    setup_conn = connect_database(database)
    try:
        migrate(setup_conn)
    finally:
        setup_conn.close()
    return create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(_default_settings()),
        trusted_hosts=trusted_hosts,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    guard: SendGuard | None = None,
    now: datetime | None = None,
    draft_service: DraftService | None = None,
    candidate_source: CandidateSource | None = None,
    site_transport: SiteTransport | None = None,
    company_verifier: CompanyVerifier | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "web":
        if not 1 <= args.port <= 65535:
            raise SystemExit("port must be between 1 and 65535")
        web_host = _validate_web_host(args.host)
        app = build_locked_web_app(trusted_hosts=(web_host,))
        display_host = f"[{web_host}]" if ":" in web_host else web_host
        print(f"GovScout review surface: http://{display_host}:{args.port}/today")
        app.run(
            host=web_host,
            port=args.port,
            debug=False,
            use_reloader=False,
        )
        return 0
    current_time = now or datetime.now(UTC)

    if args.command in {"collector-device-add", "collector-device-revoke"}:
        if conn is None:
            conn = connect_database(_default_database_path())
            migrate(conn)
        if args.command == "collector-device-add":
            try:
                credential = create_collector_device(
                    conn,
                    display_name=args.name,
                    now=current_time,
                )
            except (sqlite3.Error, ValueError) as exc:
                print(f"Collector device creation failed: {exc}")
                return 2
            print(f"Collector device created: {credential.device_id}")
            print(f"Device token: {credential.token}")
            print("This token is shown once; store it in the collector's secure setup screen.")
            return 0
        try:
            revoke_collector_device(
                conn,
                device_id=args.device_id,
                now=current_time,
            )
        except (KeyError, sqlite3.Error, ValueError) as exc:
            print(f"Collector device revocation failed: {exc}")
            return 2
        print(f"Collector device revoked: {args.device_id}")
        return 0

    if args.command == "retire-lca":
        if conn is None:
            conn = connect_database(_default_database_path())
            migrate(conn)
        try:
            receipt = create_verified_backup(conn, args.backup)
            result = retire_lca_candidates(
                conn,
                backup_receipt=receipt,
                now=current_time,
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            print(f"LCA retirement failed: {exc}")
            return 2
        print(
            f"Retired {result.retired_count} LCA candidates; "
            f"verified backup: {receipt.backup_path} ({receipt.backup_sha256})"
        )
        return 0

    if args.command == "ingest-fca":
        if conn is None:
            conn = connect_database(_default_database_path())
            migrate(conn)
        try:
            with args.input.open("rb") as source_file:
                payload = source_file.read(FCA_MAX_RESPONSE_BYTES + 1)
            records = parse_fca_json(payload)
            result = ingest_fca_records(
                conn, records, limit=args.limit, now=current_time
            )
        except (FcaDataError, OSError, ValueError) as exc:
            print(f"FCA ingestion failed: {exc}")
            return 2
        unchanged = result.staged_count - result.created_count - result.changed_count
        print(
            f"FCA source: {result.source_count} firms; staged {result.staged_count} "
            f"({result.created_count} new, {result.changed_count} changed, "
            f"{unchanged} unchanged)"
        )
        return 0

    if args.command == "fca-firms":
        if conn is None:
            conn = connect_database(_default_database_path())
            migrate(conn)
        if not 1 <= args.limit <= 100:
            print("FCA firm list limit must be between 1 and 100")
            return 2
        rows = conn.execute(
            """
            SELECT f.frn, f.fca_status, f.firm_name, f.source_location,
                e.score, e.temperature
            FROM fca_firms f
            LEFT JOIN enrichment_runs e ON e.id = (
                SELECT id FROM enrichment_runs
                WHERE firm_id = f.id ORDER BY id DESC LIMIT 1
            )
            ORDER BY f.frn LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        if not rows:
            print("No FCA firms")
            return 0
        for row in rows:
            location = row["source_location"] or "Location not listed"
            score = (
                f"{row['score']} {row['temperature']}"
                if row["score"] is not None
                else "unscored"
            )
            print(
                f"{row['frn']} | {row['fca_status']} | {row['firm_name']} | "
                f"{location} | {score}"
            )
        return 0

    if args.command == "process-fca-queue":
        if conn is None:
            conn = connect_database(_default_database_path())
            migrate(conn)
        try:
            verifier = company_verifier or _default_company_verifier()
            result = run_pending_jobs(
                conn,
                companies_house=verifier,
                site_transport=site_transport or UrlSiteTransport(),
                now=current_time,
                limit=args.limit,
                now_provider=(lambda: datetime.now(UTC)) if now is None else None,
            )
        except (sqlite3.Error, ValueError) as exc:
            print(f"FCA queue processing failed: {exc}")
            return 2
        print(
            f"FCA queue: claimed {result.claimed}; succeeded {result.succeeded}; "
            f"failed {result.failed}; retried {result.retried}"
        )
        return 0

    if args.command == "enqueue-fca-history":
        if conn is None:
            conn = connect_database(_default_database_path())
            migrate(conn)
        try:
            result = enqueue_historical_collector_imports(
                conn,
                limit=args.limit,
                now=current_time,
                dry_run=args.dry_run,
            )
        except (FcaDataError, sqlite3.Error, ValueError) as exc:
            print(f"Historical FCA enqueue failed: {exc}")
            return 2
        suffix = " (dry run)" if args.dry_run else ""
        print(
            f"Historical FCA queue: eligible {result.eligible_count}; "
            f"enqueued {result.enqueued_count}{suffix}"
        )
        return 0

    if args.command in {
        "enrich-fca",
        "qc-fca",
        "verify-fca",
        "reverify-fca",
        "process-fca",
        "promote-fca-contact",
    }:
        if conn is None:
            conn = connect_database(_default_database_path())
            migrate(conn)
        if args.command in {
            "verify-fca",
            "reverify-fca",
            "process-fca",
            "promote-fca-contact",
        }:
            try:
                verifier = company_verifier or _default_company_verifier()
                if args.command == "process-fca":
                    result = process_firm(
                        conn,
                        firm_id=args.firm_id,
                        companies_house=verifier,
                        site_transport=site_transport or UrlSiteTransport(),
                        now=current_time,
                    )
                    if result.qc.passed:
                        print(
                            "FCA processing complete: "
                            f"verification {result.verification.attempt_id}; "
                            f"enrichment {result.enrichment.run_id}; "
                            f"QC pass {result.qc.qc_run_id}"
                        )
                        return 0
                    print(
                        "FCA processing stopped at QC: "
                        f"{', '.join(result.qc.reasons)}"
                    )
                    return 2
                if args.command == "promote-fca-contact":
                    lead_id = verify_and_promote_firm(
                        conn,
                        firm_id=args.firm_id,
                        companies_house=verifier,
                        contact_email=args.contact_email,
                        now=current_time,
                    )
                    print(f"FCA contact attached: lead {lead_id}")
                    return 0
                verification = verify_firm(
                    conn,
                    firm_id=args.firm_id,
                    companies_house=verifier,
                    now=current_time,
                    force_refresh=args.command == "reverify-fca",
                )
            except (
                CompaniesHouseTransportError,
                FcaEligibilityError,
                KeyError,
                SiteFetchError,
                sqlite3.Error,
                ValueError,
            ) as exc:
                print(f"FCA processing failed: {exc}")
                return 2
            action = "reverified" if args.command == "reverify-fca" else "verified"
            suffix = " (current receipt reused)" if verification.reused else ""
            print(f"FCA firm {action}: attempt {verification.attempt_id}{suffix}")
            return 0
        if args.command == "enrich-fca":
            try:
                result = run_enrichment(
                    conn,
                    firm_id=args.firm_id,
                    transport=site_transport or UrlSiteTransport(),
                    now=current_time,
                )
            except (KeyError, SiteFetchError, ValueError) as exc:
                print(f"FCA enrichment failed: {exc}")
                return 2
            print(
                f"FCA enrichment complete: run {result.run_id}; "
                f"{result.score} {result.temperature}"
            )
            return 0
        try:
            result = run_qc(conn, firm_id=args.firm_id, now=current_time)
        except (KeyError, ValueError) as exc:
            print(f"FCA QC failed to run: {exc}")
            return 2
        if result.passed:
            print(f"QC pass: run {result.qc_run_id}")
            return 0
        print(f"QC fail: run {result.qc_run_id}; {', '.join(result.reasons)}")
        return 2

    if conn is None or guard is None:
        conn, guard = _default_dependencies()

    if args.command == "sends":
        decision = guard.status(conn, now=current_time)
        if args.week:
            print(f"Last 7 UK days: {guard.week_count(conn, current_time)} countable drafts")
        print(format_counter(decision))
        return 0

    print(format_counter(guard.status(conn, now=current_time)))
    if args.command in {"draft", "draft-batch"} and (
        draft_service is None or candidate_source is None
    ):
        print("Drafting locked: LINT_NOT_READY")
        return 2

    if args.command == "draft":
        assert draft_service is not None
        assert candidate_source is not None
        try:
            request = candidate_source.get(args.lead_id)
            result = draft_service.create_review_draft(conn, request, now=current_time)
        except (KeyError, StopIteration):
            print("Lead is not due or does not exist")
            return 2
        except DraftPolicyRefused as exc:
            print(f"Draft refused: {', '.join(exc.reasons)}")
            return 2
        except SendLimitExceeded:
            print("Draft refused: effective daily hard limit reached")
            return 3
        except (DraftOutcomeUncertain, DraftAlreadySent, ReservationConflict) as exc:
            print(f"Draft refused: {exc}")
            return 4
        outcome = "created" if result.created else "reused"
        print(
            f"Draft {outcome}: ledger {result.send_id}, Gmail draft {result.draft_id}"
        )
        return 0

    if args.command == "draft-batch":
        assert draft_service is not None
        assert candidate_source is not None
        candidates = sorted(
            candidate_source.due(),
            key=lambda request: (request.stage == 0, request.lead_id),
        )
        drafted = 0
        processed = 0
        for request in candidates:
            try:
                result = draft_service.create_review_draft(
                    conn,
                    request,
                    now=current_time,
                )
            except SendLimitExceeded:
                break
            except DraftPolicyRefused as exc:
                print(f"Batch stopped by lint: {', '.join(exc.reasons)}")
                return 2
            except (
                DraftOutcomeUncertain,
                DraftAlreadySent,
                ReservationConflict,
            ) as exc:
                print(f"Batch stopped: {exc}")
                return 4
            processed += 1
            if result.created:
                drafted += 1
        print(f"Batch drafted {drafted}; rolled over {len(candidates) - processed}")
        return 0

    if args.command == "send-undo":
        if draft_service is None:
            print("Undo unavailable: Gmail draft adapter is not configured")
            return 2
        try:
            draft_service.undo_draft(conn, send_id=args.send_id, now=current_time)
        except ValueError as exc:
            print(f"Undo refused: {exc}")
            return 2
        print(f"Draft voided: ledger {args.send_id}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
