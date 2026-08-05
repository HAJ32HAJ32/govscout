import json
from urllib.error import HTTPError

import pytest

from govscout.companies_house_http import (
    CompaniesHouseHttpTransport,
    CompaniesHouseTransportError,
)


class JsonResponse:
    status = 200

    def __init__(self, payload, *, content_type="application/json"):
        self.headers = {"Content-Type": content_type}
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit):
        return self._body[:limit]


class RecordingOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_transport_fetches_exact_company_profile_without_putting_secret_in_url():
    opener = RecordingOpener(
        JsonResponse(
            {
                "company_number": "00123456",
                "company_name": "Example Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    )

    profile = CompaniesHouseHttpTransport(api_key="top-secret", opener=opener).get_company_profile(
        "00123456"
    )

    request, timeout = opener.calls[0]
    assert request.full_url == "https://api.company-information.service.gov.uk/company/00123456"
    assert request.method == "GET"
    assert "top-secret" not in request.full_url
    assert request.headers["Authorization"].startswith("Basic ")
    assert request.headers["Accept"] == "application/json"
    assert timeout == 10.0
    assert profile["company_number"] == "00123456"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (JsonResponse({}, content_type="text/html"), "INVALID_CONTENT_TYPE"),
        (HTTPError("https://example.test", 302, "redirect", {}, None), "REDIRECT_REFUSED"),
        (HTTPError("https://example.test", 404, "missing", {}, None), "NOT_FOUND"),
        (HTTPError("https://example.test", 429, "limited", {}, None), "TEMPORARILY_UNAVAILABLE"),
    ],
)
def test_transport_fails_closed_with_controlled_reason_codes(response, code):
    with pytest.raises(CompaniesHouseTransportError, match=code):
        CompaniesHouseHttpTransport(api_key="top-secret", opener=RecordingOpener(response)).get_company_profile(
            "00123456"
        )


def test_transport_rejects_oversized_profile_body():
    class OversizedResponse(JsonResponse):
        def read(self, limit):
            return b"{" + (b"x" * limit)

    with pytest.raises(CompaniesHouseTransportError, match="RESPONSE_TOO_LARGE"):
        CompaniesHouseHttpTransport(
            api_key="top-secret", opener=RecordingOpener(OversizedResponse({}))
        ).get_company_profile("00123456")


def test_transport_rejects_invalid_company_number_before_network():
    opener = RecordingOpener(JsonResponse({}))

    with pytest.raises(ValueError, match="invalid"):
        CompaniesHouseHttpTransport(api_key="top-secret", opener=opener).get_company_profile(
            "../secrets"
        )

    assert opener.calls == []
