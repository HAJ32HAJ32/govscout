import json
from datetime import UTC, datetime

from govscout.cli import main
from govscout.companies_house import CompaniesHouseClient
from govscout.db import connect_database, migrate
from govscout.enrichment import SitePage
from govscout.fca_pipeline import verify_and_promote_firm
from tests.support import StubCompaniesHouseTransport


def test_cli_ingests_bounded_fca_export_and_lists_firms_without_creating_leads(
    tmp_path, capsys, monkeypatch
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    source = tmp_path / "fca-export.json"
    source.write_text(
        json.dumps(
            {
                "firms": [
                    {
                        "frn": f"{frn}",
                        "firm_name": f"Firm {frn} Ltd",
                        "status": "Authorised",
                        "firm_type": "Regulated firm",
                        "source_url": f"https://register.fca.org.uk/s/firm?id={frn}",
                        "website_url": f"https://firm-{frn}.test/",
                        "location": location,
                        "company_number": f"{int(frn):08d}",
                    }
                    for frn, location in (
                        ("123456", "London"),
                        ("234567", "Bristol"),
                        ("345678", "Leeds"),
                    )
                ]
            }
        )
    )
    monkeypatch.setattr(
        "govscout.cli._default_dependencies",
        lambda: (_ for _ in ()).throw(AssertionError("guard dependency was loaded")),
    )
    now = datetime(2026, 7, 25, 14, tzinfo=UTC)

    ingest_exit = main(
        ["ingest-fca", "--input", str(source), "--limit", "2"], conn=conn, now=now
    )
    ingest_output = capsys.readouterr().out
    list_exit = main(["fca-firms", "--limit", "5"], conn=conn, now=now)
    list_output = capsys.readouterr().out

    assert ingest_exit == 0
    assert ingest_output.strip() == (
        "FCA source: 3 firms; staged 2 (2 new, 0 changed, 0 unchanged)"
    )
    assert list_exit == 0
    assert "123456 | Authorised | Firm 123456 Ltd | London | unscored" in list_output
    assert "345678 | Authorised | Firm 345678 Ltd | Leeds | unscored" in list_output
    assert conn.execute("SELECT count(*) FROM fca_firms").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


def test_cli_has_no_lca_harvest_command():
    parser = __import__("govscout.cli", fromlist=["build_parser"]).build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert "harvest-lca" not in choices
    assert "candidates" not in choices


def test_cli_enqueues_historical_collector_imports_with_a_bounded_command(tmp_path, capsys):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    exit_code = main(
        ["enqueue-fca-history", "--limit", "25", "--dry-run"],
        conn=conn,
        now=datetime(2026, 8, 5, 15, tzinfo=UTC),
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == (
        "Historical FCA queue: eligible 0; enqueued 0 (dry run)"
    )


def test_cli_creates_and_revokes_a_scoped_collector_device(tmp_path, capsys):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    now = datetime(2026, 7, 30, 11, tzinfo=UTC)

    assert main(
        ["collector-device-add", "--name", "H Mac laptop"],
        conn=conn,
        now=now,
    ) == 0
    output = capsys.readouterr().out
    conn.close()
    conn = connect_database(database)
    device_id = conn.execute("SELECT device_id FROM collector_devices").fetchone()[0]
    token_line = next(line for line in output.splitlines() if line.startswith("Device token: "))
    token = token_line.removeprefix("Device token: ")
    assert device_id in output
    assert token.startswith(f"gsc_{device_id}_")
    assert token not in conn.execute("SELECT token_hash FROM collector_devices").fetchone()[0]

    assert main(
        ["collector-device-revoke", device_id],
        conn=conn,
        now=now,
    ) == 0
    assert "Collector device revoked" in capsys.readouterr().out
    conn.close()
    conn = connect_database(database)
    assert conn.execute(
        "SELECT revoked_at FROM collector_devices WHERE device_id = ?", (device_id,)
    ).fetchone()[0] == now.isoformat()


def test_cli_runs_repeatable_enrichment_and_qc_for_one_fca_firm(tmp_path, capsys):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    source = tmp_path / "fca-export.json"
    source.write_text(
        json.dumps(
            {
                "firms": [
                    {
                        "frn": "123456",
                        "firm_name": "Example Finance Ltd",
                        "status": "Authorised",
                        "firm_type": "Regulated firm",
                        "source_url": "https://register.fca.org.uk/s/firm?id=123456",
                        "website_url": "https://example.test/",
                        "location": "London",
                        "company_number": "12345678",
                    }
                ]
            }
        )
    )
    now = datetime(2026, 7, 25, 14, tzinfo=UTC)
    assert main(["ingest-fca", "--input", str(source)], conn=conn, now=now) == 0
    capsys.readouterr()
    verify_and_promote_firm(
        conn,
        firm_id=1,
        companies_house=CompaniesHouseClient(
            StubCompaniesHouseTransport(
                {
                    "company_number": "12345678",
                    "company_name": "Example Finance Ltd",
                    "company_status": "active",
                    "type": "ltd",
                }
            )
        ),
        contact_email="director@example.test",
        now=now,
    )

    class SiteTransport:
        def fetch_html(self, url):
            html = {
                "https://example.test/": "FCA regulated AI-powered advice.",
                "https://example.test/privacy": "Privacy and cookies only.",
                "https://example.test/careers": "We use Copilot.",
                "https://example.test/ai-policy": "Our AI governance policy.",
            }[url]
            return SitePage(url=url, final_url=url, html=html, fetched_at=now)

    enrich_exit = main(
        ["enrich-fca", "1"], conn=conn, now=now, site_transport=SiteTransport()
    )
    enrich_output = capsys.readouterr().out
    qc_exit = main(["qc-fca", "1"], conn=conn, now=now)
    qc_output = capsys.readouterr().out

    assert enrich_exit == 0
    assert "85 HOT" in enrich_output
    assert qc_exit == 0
    assert "QC pass" in qc_output


def test_cli_processes_verified_firm_to_qc_without_inventing_contact(tmp_path, capsys):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    source = tmp_path / "fca-export.json"
    source.write_text(
        json.dumps(
            {
                "firms": [
                    {
                        "frn": "123456",
                        "firm_name": "Example Finance Ltd",
                        "status": "Authorised",
                        "firm_type": "Regulated firm",
                        "source_url": "https://register.fca.org.uk/s/firm?id=123456",
                        "website_url": "https://example.test/",
                        "location": "London",
                        "company_number": "12345678",
                    }
                ]
            }
        )
    )
    now = datetime(2026, 8, 5, 10, tzinfo=UTC)
    assert main(["ingest-fca", "--input", str(source)], conn=conn, now=now) == 0
    capsys.readouterr()
    ch_transport = StubCompaniesHouseTransport(
        {
            "company_number": "12345678",
            "company_name": "Example Finance Ltd",
            "company_status": "active",
            "type": "ltd",
        }
    )

    class SiteTransport:
        def fetch_html(self, url):
            html = {
                "https://example.test/": "FCA regulated AI-powered advice.",
                "https://example.test/privacy": "Privacy and automated decisions.",
                "https://example.test/careers": "We use Copilot.",
                "https://example.test/ai-policy": "Our AI governance policy.",
            }[url]
            return SitePage(url=url, final_url=url, html=html, fetched_at=now)

    exit_code = main(
        ["process-fca", "1"],
        conn=conn,
        now=now,
        company_verifier=CompaniesHouseClient(ch_transport),
        site_transport=SiteTransport(),
    )

    assert exit_code == 0
    assert "QC pass" in capsys.readouterr().out
    assert ch_transport.requests == ["12345678"]
    assert conn.execute("SELECT count(*) FROM company_verification_attempts").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM qc_runs ORDER BY id DESC").fetchone()[0] == "pass"


def test_cli_can_add_real_contact_after_legal_verification(tmp_path, capsys):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    source = tmp_path / "fca-export.json"
    source.write_text(
        json.dumps(
            {
                "firms": [
                    {
                        "frn": "123456",
                        "firm_name": "Example Finance Ltd",
                        "status": "Authorised",
                        "source_url": "https://register.fca.org.uk/s/firm?id=123456",
                        "website_url": "https://example.test/",
                        "location": "London",
                        "company_number": "12345678",
                    }
                ]
            }
        )
    )
    now = datetime(2026, 8, 5, 10, tzinfo=UTC)
    assert main(["ingest-fca", "--input", str(source)], conn=conn, now=now) == 0
    capsys.readouterr()
    verifier = CompaniesHouseClient(
        StubCompaniesHouseTransport(
            {
                "company_number": "12345678",
                "company_name": "Example Finance Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    )

    exit_code = main(
        ["promote-fca-contact", "1", "--contact-email", "compliance@example.test"],
        conn=conn,
        now=now,
        company_verifier=verifier,
    )

    assert exit_code == 0
    assert "contact attached" in capsys.readouterr().out.lower()
    row = conn.execute(
        """
        SELECT l.contact_email FROM fca_firms f
        JOIN leads l ON l.id = f.lead_id WHERE f.id = 1
        """
    ).fetchone()
    assert row[0] == "compliance@example.test"


def test_cli_retires_lca_candidates_only_after_creating_verified_backup(
    tmp_path, capsys, monkeypatch
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    conn.execute(
        """
        INSERT INTO candidates (
            source_register, source_url, company_name, source_record_hash,
            discovered_at, last_seen_at
        ) VALUES ('LCA member directory', ?, 'Legacy Ltd', ?, ?, ?)
        """,
        (
            "https://www.legionellacontrolassociation.co.uk/company/legacy/",
            "a" * 64,
            "2026-07-25T10:00:00+00:00",
            "2026-07-25T10:00:00+00:00",
        ),
    )
    backup = tmp_path / "retirement-backup.sqlite3"
    monkeypatch.setattr(
        "govscout.cli._default_dependencies",
        lambda: (_ for _ in ()).throw(AssertionError("guard dependency was loaded")),
    )

    exit_code = main(
        ["retire-lca", "--backup", str(backup)],
        conn=conn,
        now=datetime(2026, 7, 25, 14, tzinfo=UTC),
    )

    assert exit_code == 0
    assert backup.is_file()
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
    assert "Retired 1 LCA candidates" in capsys.readouterr().out
