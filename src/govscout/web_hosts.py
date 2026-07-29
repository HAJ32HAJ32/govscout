from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
import re


TAILSCALE_IPV4 = ip_network("100.64.0.0/10")
TAILSCALE_IPV6 = ip_network("fd7a:115c:a1e0::/48")
_BRACKETED_HOST = re.compile(r"^\[([^\]]+)\](?::([0-9]+))?$")
_DNS_HOST = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def canonical_safe_bind_host(value: str) -> str:
    """Canonicalise one IP literal and reject non-loopback/non-Tailscale binds."""
    if not isinstance(value, str) or value != value.strip() or "%" in value:
        raise ValueError("host must be a loopback or Tailscale IP literal")
    try:
        address = ip_address(value)
    except ValueError as exc:
        raise ValueError("host must be a loopback or Tailscale IP literal") from exc
    allowed = address.is_loopback
    if isinstance(address, IPv4Address):
        allowed = allowed or address in TAILSCALE_IPV4
    elif isinstance(address, IPv6Address):
        allowed = allowed or address in TAILSCALE_IPV6
    if not allowed:
        raise ValueError("host must be a loopback or Tailscale IP literal")
    return str(address)


def parse_host_header(value: str) -> str | None:
    """Strictly parse a raw HTTP Host header into a canonical host name/IP."""
    if not isinstance(value, str) or not value or value != value.strip():
        return None

    bracketed = _BRACKETED_HOST.fullmatch(value)
    if bracketed:
        host_text, port_text = bracketed.groups()
        if "%" in host_text or not _valid_port(port_text):
            return None
        try:
            address = ip_address(host_text)
        except ValueError:
            return None
        if not isinstance(address, IPv6Address):
            return None
        return str(address)

    if "[" in value or "]" in value or value.count(":") > 1:
        return None
    host_text, separator, port_text = value.partition(":")
    if separator and not _valid_port(port_text):
        return None
    if host_text.lower() == "localhost":
        return "localhost"
    if "%" in host_text:
        return None
    try:
        address = ip_address(host_text)
    except ValueError:
        canonical_name = host_text.lower()
        return canonical_name if _DNS_HOST.fullmatch(canonical_name) else None
    if not isinstance(address, IPv4Address):
        return None
    return str(address)


def _valid_port(port_text: str | None) -> bool:
    if port_text is None:
        return True
    return (
        bool(port_text)
        and port_text.isascii()
        and port_text.isdecimal()
        and 1 <= int(port_text) <= 65535
    )
