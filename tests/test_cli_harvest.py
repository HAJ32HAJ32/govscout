from datetime import UTC, datetime
import json

from govscout.cli import main
from govscout.db import connect_database, migrate
from govscout.enrichment import SitePage


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
