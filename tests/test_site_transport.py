from datetime import UTC, datetime
from email.message import Message
import socket
from urllib.error import HTTPError

import pytest

from govscout.enrichment import (
    SITE_MAX_RESPONSE_BYTES,
    SITE_TIMEOUT_SECONDS,
    SITE_USER_AGENT,
    SiteFetchError,
    UrlSiteTransport,
)


URL = "https://example.com/privacy"
NOW = datetime(2026, 7, 25, 10, tzinfo=UTC)


def _resolver(_host, _port, *, type):
    assert type == socket.SOCK_STREAM
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


class Response:
    def __init__(
        self,
        *,
        url=URL,
        status=200,
        content_type="text/html; charset=utf-8",
        payload=b"<p>ok</p>",
    ):
        self.url = url
        self.status = status
        self.payload = payload
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(payload))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, size):
        assert size == SITE_MAX_RESPONSE_BYTES + 1
        return self.payload


def test_site_transport_uses_https_public_address_timeout_size_and_identifiable_agent():
    calls = []

    def opener(request, *, timeout, pinned_ip, server_hostname):
        calls.append((request, timeout, pinned_ip, server_hostname))
        return Response()

    page = UrlSiteTransport(
        opener=opener, resolver=_resolver, now_provider=lambda: NOW
    ).fetch_html(URL)

    assert page.html == "<p>ok</p>"
    request, timeout, pinned_ip, server_hostname = calls[0]
    assert request.full_url == URL
    assert request.get_header("User-agent") == SITE_USER_AGENT
    assert request.get_header("Host") == "example.com"
    assert pinned_ip == "93.184.216.34"
    assert server_hostname == "example.com"
    assert timeout == SITE_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("url", "addresses", "error"),
    [
        ("http://example.com/", ["93.184.216.34"], "UNSAFE_URL"),
        ("https://user@example.com/", ["93.184.216.34"], "UNSAFE_URL"),
        ("https://example.com/#fragment", ["93.184.216.34"], "UNSAFE_URL"),
        ("https://example.com/", ["127.0.0.1"], "NON_PUBLIC_ADDRESS"),
        ("https://example.com/", ["10.0.0.2"], "NON_PUBLIC_ADDRESS"),
        ("https://example.com/", ["169.254.169.254"], "NON_PUBLIC_ADDRESS"),
    ],
)
def test_site_transport_rejects_unsafe_urls_and_non_public_dns(url, addresses, error):
    def resolver(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses
        ]

    transport = UrlSiteTransport(
        opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("opened")),
        resolver=resolver,
    )

    with pytest.raises(SiteFetchError, match=error):
        transport.fetch_html(url)


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (Response(url="https://example.com/redirected"), "REDIRECTED"),
        (Response(content_type="application/json"), "UNSUPPORTED_CONTENT_TYPE"),
        (Response(payload=b"x" * (SITE_MAX_RESPONSE_BYTES + 1)), "RESPONSE_TOO_LARGE"),
    ],
)
def test_site_transport_rejects_redirect_wrong_content_type_and_oversize(response, error):
    transport = UrlSiteTransport(
        opener=lambda _request, **_kwargs: response,
        resolver=_resolver,
        now_provider=lambda: NOW,
    )

    with pytest.raises(SiteFetchError, match=error):
        transport.fetch_html(URL)


def test_site_transport_classifies_http_404_as_not_found():
    def opener(_request, **_kwargs):
        raise HTTPError(URL, 404, "Not Found", Message(), None)

    transport = UrlSiteTransport(opener=opener, resolver=_resolver)

    with pytest.raises(SiteFetchError, match="^NOT_FOUND$"):
        transport.fetch_html(URL)


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, "NOT_FOUND"), (401, "FETCH_FAILED"), (500, "FETCH_FAILED")],
)
def test_site_transport_classifies_non_success_response_objects(status, error):
    transport = UrlSiteTransport(
        opener=lambda _request, **_kwargs: Response(status=status, payload=b"<p>Error</p>"),
        resolver=_resolver,
    )

    with pytest.raises(SiteFetchError, match=f"^{error}$"):
        transport.fetch_html(URL)
