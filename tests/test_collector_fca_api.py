import json
from urllib.parse import parse_qs, urlsplit

import pytest

from govscout.fca_discovery import parse_fca_json
from govscout_collector.fca_api import FcaApiError, FcaRegisterClient


class JsonResponse:
    status = 200

    def __init__(self, payload):
        self.headers = {"Content-Type": "application/json"}
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit):
        return self._payload[:limit]


class FcaApiOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        parsed = urlsplit(request.full_url)
        if parsed.path.endswith("/Search"):
            assert parse_qs(parsed.query) == {"q": ["finance"], "type": ["firm"]}
            return JsonResponse(
                {
                    "Status": "FSR-API-04-01-00",
                    "Message": "Ok. Search successful",
                    "Data": [
                        {"Reference Number": "123456", "Name": "Alpha Finance Ltd"},
                        {"Reference Number": "654321", "Name": "Beta Finance Ltd"},
                        {"Reference Number": "777777", "Name": "Gamma Finance Ltd"},
                    ],
                }
            )
        if parsed.path.endswith("/Firm/123456"):
            return JsonResponse(
                {
                    "Status": "FSR-API-02-01-00",
                    "Message": "Ok. Firm found",
                    "Data": [
                        {
                            "FRN": "123456",
                            "Organisation Name": "Alpha Finance Ltd",
                            "Status": "Authorised",
                            "Business Type": "Regulated",
                            "Companies House Number": "00123456",
                        }
                    ],
                }
            )
        if parsed.path.endswith("/Firm/123456/Address"):
            return JsonResponse(
                {
                    "Status": "FSR-API-02-02-00",
                    "Message": "Ok. Address found",
                    "Data": [
                        {
                            "Address Type": "Principal Place of Business",
                            "Website Address": "https://alpha.example/",
                            "Town": "London",
                            "Postcode": "SW1A 1AA",
                        }
                    ],
                }
            )
        if parsed.path.endswith("/Firm/654321"):
            return JsonResponse(
                {
                    "Status": "FSR-API-02-01-00",
                    "Message": "Ok. Firm found",
                    "Data": [
                        {
                            "FRN": "654321",
                            "Organisation Name": "Beta Finance Ltd",
                            "Status": "Cancelled",
                            "Business Type": "Regulated",
                        }
                    ],
                }
            )
        if parsed.path.endswith("/Firm/777777"):
            return JsonResponse(
                {
                    "Status": "FSR-API-02-01-00",
                    "Message": "Ok. Firm found",
                    "Data": [
                        {
                            "FRN": "777777",
                            "Organisation Name": "Gamma Finance Ltd",
                            "Status": "Registered",
                            "Business Type": "Regulated",
                        }
                    ],
                }
            )
        if parsed.path.endswith("/Firm/777777/Address"):
            return JsonResponse(
                {
                    "Status": "FSR-API-02-02-00",
                    "Message": "Ok. Address found",
                    "Data": [],
                }
            )
        raise AssertionError(request.full_url)


def test_official_fca_client_collects_active_firms_into_bounded_govscout_payload():
    opener = FcaApiOpener()
    delays = []
    client = FcaRegisterClient(opener=opener, sleeper=delays.append)

    payload = client.collect(
        search_terms=("finance",),
        limit=2,
        email="operator@example.test",
        api_key="secret-api-key",
    )

    records = parse_fca_json(payload)
    assert len(records) == 2
    assert records[0].frn == "123456"
    assert records[0].firm_name == "Alpha Finance Ltd"
    assert records[0].fca_status == "Authorised"
    assert records[0].website_url == "https://alpha.example/"
    assert records[0].source_location == "London, SW1A 1AA"
    assert records[0].company_number == "00123456"
    assert records[1].frn == "777777"
    assert all(request.method == "GET" for request, _timeout in opener.requests)
    assert all(request.headers["X-auth-email"] == "operator@example.test" for request, _ in opener.requests)
    assert all(request.headers["X-auth-key"] == "secret-api-key" for request, _ in opener.requests)
    assert all("secret-api-key" not in request.full_url for request, _ in opener.requests)
    assert len(delays) == len(opener.requests) - 1


@pytest.mark.parametrize(
    "identity_fields",
    [
        {},
        {"Companies House Number": "not-a-number"},
        {"Companies House Number": 12345678},
        {"Mutual Society Number": "12345678"},
        {
            "Companies House Number": "12345678",
            "Mutual Society Number": "MUTUAL-1",
        },
    ],
)
def test_fca_client_does_not_promote_absent_malformed_or_ambiguous_identity(identity_fields):
    class Opener:
        def open(self, request, timeout):
            path = urlsplit(request.full_url).path
            if path.endswith("/Search"):
                return JsonResponse(
                    {
                        "Status": "FSR-API-04-01-00",
                        "Message": "Ok. Search successful",
                        "Data": [{"Reference Number": "123456"}],
                    }
                )
            if path.endswith("/Address"):
                return JsonResponse(
                    {
                        "Status": "FSR-API-02-02-00",
                        "Message": "Ok. Address found",
                        "Data": [],
                    }
                )
            return JsonResponse(
                {
                    "Status": "FSR-API-02-01-00",
                    "Message": "Ok. Firm found",
                    "Data": [
                        {
                            "FRN": "123456",
                            "Organisation Name": "Alpha Finance Ltd",
                            "Status": "Authorised",
                            **identity_fields,
                        }
                    ],
                }
            )

    payload = FcaRegisterClient(opener=Opener(), sleeper=lambda _seconds: None).collect(
        search_terms=("finance",),
        limit=1,
        email="operator@example.test",
        api_key="secret-api-key",
    )

    assert parse_fca_json(payload)[0].company_number is None


@pytest.mark.parametrize(
    "details",
    [
        {
            "Status": "FSR-API-02-01-99",
            "Message": "Firm request failed",
            "Data": [],
        },
        {
            "Status": "FSR-API-02-01-00",
            "Message": "Ok. Firm found",
            "Data": [{"Organisation Name": "No identity"}],
        },
    ],
)
def test_fca_client_fails_closed_on_api_error_status_or_missing_returned_frn(details):
    class Opener:
        def open(self, request, timeout):
            if urlsplit(request.full_url).path.endswith("/Search"):
                return JsonResponse(
                    {
                        "Status": "FSR-API-04-01-00",
                        "Message": "Ok. Search successful",
                        "Data": [{"Reference Number": "123456"}],
                    }
                )
            return JsonResponse(details)

    with pytest.raises(FcaApiError):
        FcaRegisterClient(opener=Opener(), sleeper=lambda _seconds: None).collect(
            search_terms=("finance",),
            limit=1,
            email="operator@example.test",
            api_key="secret-api-key",
        )


def test_fca_client_searches_beyond_four_inactive_candidates_for_one_active_firm():
    inactive_frns = tuple(f"{number:06d}" for number in range(100000, 100004))
    active_frn = "100004"

    class Opener:
        def open(self, request, timeout):
            path = urlsplit(request.full_url).path
            if path.endswith("/Search"):
                return JsonResponse(
                    {
                        "Status": "FSR-API-04-01-00",
                        "Message": "Ok. Search successful",
                        "Data": [
                            {"Reference Number": frn}
                            for frn in (*inactive_frns, active_frn)
                        ],
                    }
                )
            frn = path.split("/")[-1]
            if path.endswith("/Address"):
                return JsonResponse(
                    {
                        "Status": "FSR-API-02-02-00",
                        "Message": "Ok. Address found",
                        "Data": [],
                    }
                )
            return JsonResponse(
                {
                    "Status": "FSR-API-02-01-00",
                    "Message": "Ok. Firm found",
                    "Data": [
                        {
                            "FRN": frn,
                            "Organisation Name": f"Firm {frn}",
                            "Status": "Registered" if frn == active_frn else "Cancelled",
                        }
                    ],
                }
            )

    payload = FcaRegisterClient(
        opener=Opener(), sleeper=lambda _seconds: None
    ).collect(
        search_terms=("finance",),
        limit=1,
        email="operator@example.test",
        api_key="secret-api-key",
    )

    assert [record.frn for record in parse_fca_json(payload)] == [active_frn]
