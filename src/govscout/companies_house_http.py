from __future__ import annotations

import base64
import json
import re
import ssl
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


COMPANIES_HOUSE_API_ROOT = "https://api.company-information.service.gov.uk"
COMPANIES_HOUSE_RESPONSE_LIMIT_BYTES = 256_000
_COMPANY_NUMBER = re.compile(r"^[A-Z0-9]{8}$")


class CompaniesHouseTransportError(RuntimeError):
    """A controlled Companies House retrieval failure with no credential detail."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CompaniesHouseTransportError("REDIRECT_REFUSED")


class CompaniesHouseHttpTransport:
    """Bounded, authenticated transport for the official company-profile endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        opener=None,
        timeout: float = 10.0,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(api_key, str) or not api_key or api_key != api_key.strip():
            raise ValueError("Companies House API key is required")
        if not 1 <= max_attempts <= 3:
            raise ValueError("Companies House attempts must be between 1 and 3")
        self._authorization = "Basic " + base64.b64encode(f"{api_key}:".encode()).decode("ascii")
        self._opener = opener or build_opener(
            HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirect(),
        )
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleeper = sleeper

    def _request(self, company_number: str) -> Mapping[str, Any]:
        request = Request(
            f"{COMPANIES_HOUSE_API_ROOT}/company/{company_number}",
            method="GET",
            headers={
                "Authorization": self._authorization,
                "Accept": "application/json",
                "User-Agent": "GovScout/0.1",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if response.status != 200:
                    raise CompaniesHouseTransportError("UNEXPECTED_STATUS")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if content_type != "application/json":
                    raise CompaniesHouseTransportError("INVALID_CONTENT_TYPE")
                body = response.read(COMPANIES_HOUSE_RESPONSE_LIMIT_BYTES + 1)
        except CompaniesHouseTransportError:
            raise
        except HTTPError as exc:
            if 300 <= exc.code <= 399:
                raise CompaniesHouseTransportError("REDIRECT_REFUSED") from None
            if exc.code == 404:
                raise CompaniesHouseTransportError("NOT_FOUND") from None
            if exc.code in {401, 403}:
                raise CompaniesHouseTransportError("AUTHENTICATION_FAILED") from None
            if exc.code == 429 or 500 <= exc.code <= 599:
                raise CompaniesHouseTransportError("TEMPORARILY_UNAVAILABLE") from None
            raise CompaniesHouseTransportError("REQUEST_REFUSED") from None
        except (OSError, TimeoutError, URLError):
            raise CompaniesHouseTransportError("TEMPORARILY_UNAVAILABLE") from None
        if len(body) > COMPANIES_HOUSE_RESPONSE_LIMIT_BYTES:
            raise CompaniesHouseTransportError("RESPONSE_TOO_LARGE")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CompaniesHouseTransportError("INVALID_JSON") from None
        if type(payload) is not dict:
            raise CompaniesHouseTransportError("INVALID_PROFILE")
        return payload

    def get_company_profile(self, company_number: str) -> Mapping[str, Any]:
        requested = company_number.strip().upper()
        if not _COMPANY_NUMBER.fullmatch(requested):
            raise ValueError("Companies House company number is invalid")
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._request(requested)
            except CompaniesHouseTransportError as exc:
                if str(exc) != "TEMPORARILY_UNAVAILABLE" or attempt == self._max_attempts:
                    raise
                self._sleeper(float(2 ** (attempt - 1)))
        raise AssertionError("bounded retry loop exhausted without returning or raising")
