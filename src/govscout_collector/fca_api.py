from __future__ import annotations

import json
import re
import ssl
import time
from collections.abc import Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from govscout.fca_discovery import (
    ACTIVE_FCA_STATUSES,
    FCA_MAX_RESPONSE_BYTES,
    FcaDataError,
    canonicalize_website_url,
    parse_fca_json,
)

FCA_API_ROOT = "https://register.fca.org.uk/services/V0.1"
FCA_REQUEST_INTERVAL_SECONDS = 1.1
FCA_RESPONSE_LIMIT_BYTES = 1_000_000
FCA_MAX_CANDIDATES = 100
_SUCCESS_STATUS = re.compile(r"^FSR-API-[0-9]{2}-[0-9]{2}-00$")
_COMPANY_NUMBER = re.compile(r"^[A-Z0-9]{8}$")


class FcaApiError(RuntimeError):
    """The official FCA Register API returned unusable data."""


class FcaApiUnavailable(FcaApiError):
    """The official FCA Register API is temporarily unavailable."""


class FcaApiCredentialsRejected(FcaApiError):
    """The official FCA Register API rejected the supplied credentials."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FcaApiError("The FCA API redirected an authenticated request")


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FcaApiError(f"FCA response omitted {field}")
    return value.strip()


def _company_number_from_firm_details(details: dict[str, object]) -> str | None:
    """Return only an explicit, unambiguous Companies House identifier."""
    company_value = details.get("Companies House Number")
    mutual_value = details.get("Mutual Society Number")
    if mutual_value not in (None, ""):
        return None
    if not isinstance(company_value, str):
        return None
    company_number = company_value.strip().upper()
    return company_number if _COMPANY_NUMBER.fullmatch(company_number) else None


class FcaRegisterClient:
    def __init__(
        self,
        *,
        opener=None,
        sleeper: Callable[[float], None] = time.sleep,
        timeout: float = 20.0,
    ) -> None:
        self._opener = opener or build_opener(
            HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirect(),
        )
        self._sleeper = sleeper
        self._timeout = timeout
        self._request_count = 0

    def _request_json(self, path: str, *, email: str, api_key: str) -> dict[str, object]:
        if self._request_count:
            self._sleeper(FCA_REQUEST_INTERVAL_SECONDS)
        self._request_count += 1
        request = Request(
            f"{FCA_API_ROOT}{path}",
            method="GET",
            headers={
                "x-auth-email": email,
                "x-auth-key": api_key,
                "Accept": "application/json",
                "User-Agent": "GovScout-Collector/0.1",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                body = response.read(FCA_RESPONSE_LIMIT_BYTES + 1)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise FcaApiCredentialsRejected(
                    "The FCA API rejected the registered email or API key"
                ) from None
            if exc.code == 429 or 500 <= exc.code <= 599:
                raise FcaApiUnavailable("The FCA API is temporarily unavailable") from None
            raise FcaApiError(f"The FCA API refused the request (HTTP {exc.code})") from None
        except (OSError, TimeoutError, URLError):
            raise FcaApiUnavailable("The FCA API is temporarily unavailable") from None
        if status != 200 or content_type != "application/json" or len(body) > FCA_RESPONSE_LIMIT_BYTES:
            raise FcaApiError("The FCA API returned an invalid response")
        try:
            decoded = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise FcaApiError("The FCA API returned invalid JSON") from None
        if type(decoded) is not dict:
            raise FcaApiError("The FCA API response was not an object")
        api_status = decoded.get("Status")
        api_message = decoded.get("Message")
        if api_status == "413":
            raise FcaApiError(
                "That search term matched too many firms for the FCA Register to return. "
                "Use a narrower or more specific search term."
            )
        if (
            not isinstance(api_status, str)
            or _SUCCESS_STATUS.fullmatch(api_status) is None
            or not isinstance(api_message, str)
            or not api_message.strip()
        ):
            raise FcaApiError("The FCA API reported an unsuccessful or malformed response")
        if type(decoded.get("Data")) is not list:
            raise FcaApiError("The FCA API response did not contain a data list")
        return decoded

    def _search(self, term: str, *, email: str, api_key: str) -> tuple[str, ...]:
        query = urlencode({"q": term, "type": "firm"})
        response = self._request_json(f"/Search?{query}", email=email, api_key=api_key)
        frns: list[str] = []
        for item in response["Data"]:
            if type(item) is not dict:
                raise FcaApiError("The FCA search response contained an invalid record")
            frn = item.get("Reference Number")
            if isinstance(frn, str) and frn.isdecimal() and 6 <= len(frn) <= 8:
                frns.append(frn)
        return tuple(frns)

    def _firm_record(self, frn: str, *, email: str, api_key: str) -> dict[str, object] | None:
        details_response = self._request_json(f"/Firm/{frn}", email=email, api_key=api_key)
        details_rows = details_response["Data"]
        if len(details_rows) != 1 or type(details_rows[0]) is not dict:
            raise FcaApiError("The FCA firm response was not singular")
        details = details_rows[0]
        returned_frn = _required_text(details.get("FRN"), "firm FRN")
        if returned_frn != frn:
            raise FcaApiError("The FCA firm response did not match the requested FRN")
        status = _required_text(details.get("Status"), "firm status")
        if status not in ACTIVE_FCA_STATUSES:
            return None

        address_response = self._request_json(
            f"/Firm/{frn}/Address", email=email, api_key=api_key
        )
        address_rows = address_response["Data"]
        if any(type(item) is not dict for item in address_rows):
            raise FcaApiError("The FCA address response contained an invalid record")
        address = next(
            (
                item
                for item in address_rows
                if item.get("Address Type") == "Principal Place of Business"
            ),
            address_rows[0] if address_rows else {},
        )
        website_value = address.get("Website Address")
        try:
            website_url = canonicalize_website_url(website_value) if website_value else None
        except FcaDataError:
            website_url = None
        location_parts = [
            value.strip()
            for value in (address.get("Town"), address.get("County"), address.get("Postcode"))
            if isinstance(value, str) and value.strip()
        ]
        location = ", ".join(dict.fromkeys(location_parts)) or None
        return {
            "frn": frn,
            "firm_name": _required_text(details.get("Organisation Name"), "organisation name"),
            "status": status,
            "firm_type": (
                details["Business Type"].strip()
                if isinstance(details.get("Business Type"), str)
                and details["Business Type"].strip()
                else None
            ),
            "source_url": f"https://register.fca.org.uk/s/firm?id={frn}",
            "website_url": website_url,
            "location": location,
            "company_number": _company_number_from_firm_details(details),
        }

    def collect(
        self,
        *,
        search_terms: Sequence[str],
        limit: int,
        email: str,
        api_key: str,
    ) -> bytes:
        if not 1 <= limit <= 25:
            raise ValueError("collector limit must be between 1 and 25")
        if not isinstance(email, str) or not email.strip() or email != email.strip():
            raise ValueError("FCA registered email is required")
        if not isinstance(api_key, str) or not api_key or api_key != api_key.strip():
            raise ValueError("FCA API key is required")
        terms = tuple(
            dict.fromkeys(
                term.strip()
                for term in search_terms
                if isinstance(term, str) and term.strip()
            )
        )
        if not 1 <= len(terms) <= 5 or any(len(term) > 80 for term in terms):
            raise ValueError("enter between one and five search terms of at most 80 characters")

        frns: list[str] = []
        candidate_limit = FCA_MAX_CANDIDATES
        for term in terms:
            for frn in self._search(term, email=email, api_key=api_key):
                if frn not in frns:
                    frns.append(frn)
                if len(frns) >= candidate_limit:
                    break
            if len(frns) >= candidate_limit:
                break

        firms: list[dict[str, object]] = []
        for frn in frns:
            record = self._firm_record(frn, email=email, api_key=api_key)
            if record is not None:
                firms.append(record)
            if len(firms) >= limit:
                break
        if not firms:
            raise FcaApiError("The FCA search returned no active firms")
        payload = json.dumps(
            {"firms": firms},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        if len(payload) > FCA_MAX_RESPONSE_BYTES:
            raise FcaApiError("The collected FCA batch exceeded the upload limit")
        parse_fca_json(payload)
        return payload
