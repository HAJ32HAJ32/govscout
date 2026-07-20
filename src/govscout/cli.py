from __future__ import annotations

import argparse
from datetime import UTC, datetime
import os
from pathlib import Path
import sqlite3
from typing import Protocol, Sequence

from govscout.config import Settings, load_default_settings, load_settings
from govscout.db import connect_database, migrate
from govscout.draft_service import (
    DraftAlreadySent,
    DraftOutcomeUncertain,
    DraftPolicyRefused,
    DraftService,
)
from govscout.lca_harvest import (
    LcaDirectoryFormatError,
    LcaDirectoryTransport,
    UrlLcaDirectoryTransport,
    harvest_lca,
)
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
    harvest_lca_parser = subparsers.add_parser(
        "harvest-lca", help="stage a bounded sample from the official LCA directory"
    )
    harvest_lca_parser.add_argument("--limit", type=int, default=25)
    candidates = subparsers.add_parser(
        "candidates", help="list staged candidates awaiting verification"
    )
    candidates.add_argument("--limit", type=int, default=25)
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
    lca_transport: LcaDirectoryTransport | None = None,
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

    if args.command == "harvest-lca":
        if conn is None:
            conn = connect_database(_default_database_path())
            migrate(conn)
        transport = lca_transport or UrlLcaDirectoryTransport()
        try:
            result = harvest_lca(
                conn,
                transport,
                limit=args.limit,
                now=current_time,
            )
        except (LcaDirectoryFormatError, OSError, ValueError) as exc:
            print(f"LCA harvest failed: {exc}")
            return 2
        refreshed = result.staged_count - result.created_count
        print(
            f"LCA directory: {result.source_count} members; "
            f"staged {result.staged_count} "
            f"({result.created_count} new, {refreshed} refreshed)"
        )
        return 0

    if args.command == "candidates":
        if conn is None:
            conn = connect_database(_default_database_path())
            migrate(conn)
        if not 1 <= args.limit <= 50:
            print("Candidate list limit must be between 1 and 50")
            return 2
        rows = conn.execute(
            """
            SELECT id, status, company_name, source_location, source_url
            FROM candidates
            ORDER BY id
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        if not rows:
            print("No staged candidates")
            return 0
        for row in rows:
            location = row["source_location"] or "Location not listed"
            print(
                f"{row['id']} | {row['status']} | {row['company_name']} | "
                f"{location}\n  {row['source_url']}"
            )
        return 0

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
