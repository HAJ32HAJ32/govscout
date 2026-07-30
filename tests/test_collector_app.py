import hashlib
import json

from govscout_collector.app import CollectorService
from govscout_collector.core import CollectorQueue, UploadReceipt
from govscout_collector.credentials import CollectorCredentials
from govscout_collector.paths import default_queue_path


def _credentials():
    return CollectorCredentials(
        fca_email="operator@example.test",
        fca_api_key="fca-secret",
        upload_token="gsc_" + "a" * 32 + "_" + "b" * 43,
    )


def test_collector_service_stages_and_uploads_the_exact_official_fca_payload(tmp_path):
    payload = json.dumps(
        {
            "firms": [
                {
                    "frn": "123456",
                    "firm_name": "Example Finance Ltd",
                    "status": "Authorised",
                    "firm_type": "Regulated",
                    "source_url": "https://register.fca.org.uk/s/firm?id=123456",
                    "website_url": "https://example.test/",
                    "location": "London",
                    "company_number": None,
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
    calls = {}

    class FcaClient:
        def collect(self, **kwargs):
            calls["collect"] = kwargs
            return payload

    class Transport:
        def upload(self, *, import_id, payload, token):
            calls["upload"] = (import_id, payload, token)
            return UploadReceipt(import_id, hashlib.sha256(payload).hexdigest(), "accepted")

    queue = CollectorQueue(tmp_path / "collector.sqlite3")
    service = CollectorService(fca_client=FcaClient(), queue=queue, transport=Transport())

    result = service.collect_and_upload(
        credentials=_credentials(),
        search_terms=("finance",),
        limit=25,
    )

    assert result.uploaded == 1
    assert result.pending == 0
    assert calls["collect"] == {
        "search_terms": ("finance",),
        "limit": 25,
        "email": "operator@example.test",
        "api_key": "fca-secret",
    }
    assert calls["upload"][1:] == (payload, _credentials().upload_token)
    assert queue.pending() == ()


def test_default_queue_path_uses_native_private_application_data_locations(tmp_path):
    assert default_queue_path(
        platform="win32",
        home=tmp_path,
        local_appdata=tmp_path / "Local",
    ) == tmp_path / "Local" / "GovScout Collector" / "collector.sqlite3"
    assert default_queue_path(
        platform="darwin",
        home=tmp_path,
        local_appdata=None,
    ) == tmp_path / "Library" / "Application Support" / "GovScout Collector" / "collector.sqlite3"
