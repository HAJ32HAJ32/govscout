from datetime import UTC, datetime

from govscout.cli import main
from govscout.db import connect_database, migrate


class StaticLcaTransport:
    def fetch_html(self, _url):
        return """
        <div class="lca-az-index">
        <a class="lca-az-link"
           href="https://www.legionellacontrolassociation.co.uk/company/alpha/">
          <strong>Alpha Limited</strong><span>London</span>
        </a>
        <a class="lca-az-link"
           href="https://www.legionellacontrolassociation.co.uk/company/bravo/">
          <strong>Bravo Limited</strong><span>Bristol</span>
        </a>
        <a class="lca-az-link"
           href="https://www.legionellacontrolassociation.co.uk/company/charlie/">
          <strong>Charlie Limited</strong><span>Leeds</span>
        </a>
        <a class="lca-az-link"
           href="https://www.legionellacontrolassociation.co.uk/company/delta/">
          <strong>Delta Limited</strong><span>Glasgow</span>
        </a>
        </div>
        """


def test_cli_harvests_and_lists_staged_candidates_without_guard_or_leads(
    tmp_path, capsys, monkeypatch
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    now = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "govscout.cli._default_dependencies",
        lambda: (_ for _ in ()).throw(AssertionError("guard dependency was loaded")),
    )

    harvest_exit = main(
        ["harvest-lca", "--limit", "2"],
        conn=conn,
        now=now,
        lca_transport=StaticLcaTransport(),
    )
    harvest_output = capsys.readouterr().out

    retry_exit = main(
        ["harvest-lca", "--limit", "2"],
        conn=conn,
        now=now,
        lca_transport=StaticLcaTransport(),
    )
    retry_output = capsys.readouterr().out

    list_exit = main(
        ["candidates", "--limit", "2"],
        conn=conn,
        now=now,
    )
    list_output = capsys.readouterr().out

    assert harvest_exit == 0
    assert harvest_output.strip() == (
        "LCA directory: 4 members; staged 2 (2 new, 0 refreshed)"
    )
    assert retry_exit == 0
    assert retry_output.strip() == (
        "LCA directory: 4 members; staged 2 (0 new, 2 refreshed)"
    )
    assert list_exit == 0
    assert "1 | discovered | Alpha Limited | London" in list_output
    assert "2 | discovered | Charlie Limited | Leeds" in list_output
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0
