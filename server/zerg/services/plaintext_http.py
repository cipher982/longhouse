"""The plaintext-http rule for Runtime Host addresses.

Native clients send the device token and every transcript to the address they
are pointed at. https is always fine. Plaintext http is fine to loopback and to
Tailscale addresses (WireGuard encrypts that transport), refused to a LAN or
private address unless the user opted in, and refused everywhere else.

One rule, four clients: this module, ``engine/src/plaintext_http.rs``,
``desktop/.../PlaintextHTTP.swift`` and ``ios/Sources/Shared/Auth/PlaintextHTTP.swift``.
``schemas/plaintext-http-vectors.json`` is the shared case list all four read
in their tests; change the rule there first.

Names are trusted by their suffix, not resolved: `*.ts.net` (MagicDNS) counts as
Tailscale and `*.local` as LAN. On a network whose DNS an attacker controls, a
poisoned answer for such a name could point a client at a host outside the tailnet.
Use the 100.x address where that matters.
"""

from __future__ import annotations

import ipaddress
import os
import re
from enum import Enum
from urllib.parse import urlparse

OPT_IN_ENV = "LONGHOUSE_ALLOW_INSECURE_HTTP"
OPT_IN_FLAG = "--allow-insecure-http"

_TAILSCALE_SUFFIX = ".ts.net"
_LOCAL_SUFFIX = ".local"
_DOTTED_QUAD = re.compile(r"^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$")

_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_LAN_V4 = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"))
_LAN_V6 = tuple(ipaddress.ip_network(net) for net in ("fe80::/10", "fc00::/7"))


class HostClass(str, Enum):
    LOOPBACK = "loopback"
    TAILSCALE = "tailscale"
    LAN = "lan"
    PUBLIC = "public"


class Outcome(str, Enum):
    ALLOWED = "allowed"
    ALLOWED_WARN = "allowed_warn"
    REFUSED_LAN = "refused_lan"
    REFUSED_PUBLIC = "refused_public"
    INVALID = "invalid"

    @property
    def usable(self) -> bool:
        return self in (Outcome.ALLOWED, Outcome.ALLOWED_WARN)


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Only canonical dotted-quad IPv4 and standard IPv6 literals are addresses."""
    try:
        if ":" in host:
            return None if "%" in host else ipaddress.IPv6Address(host)
        if _DOTTED_QUAD.match(host):
            return ipaddress.IPv4Address(host)
    except ValueError:
        return None
    return None


def _classify_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> HostClass:
    # Explicit networks, not the stdlib is_* flags: those changed meaning for
    # IPv4-mapped IPv6 in 3.13, and a mapped address is deliberately "public".
    if isinstance(address, ipaddress.IPv4Address):
        if address in _LOOPBACK_V4:
            return HostClass.LOOPBACK
        if address in _TAILSCALE_V4:
            return HostClass.TAILSCALE
        if any(address in net for net in _LAN_V4):
            return HostClass.LAN
        return HostClass.PUBLIC
    if address == ipaddress.IPv6Address("::1"):
        return HostClass.LOOPBACK
    if address in _TAILSCALE_V6:
        return HostClass.TAILSCALE
    if any(address in net for net in _LAN_V6):
        return HostClass.LAN
    return HostClass.PUBLIC


def classify_host(host: str) -> HostClass:
    """Classify a URL host: case-folded, one trailing dot removed."""
    host = host.strip().lower()
    if host.endswith("."):
        host = host[:-1]
    if host == "localhost":
        return HostClass.LOOPBACK
    address = _parse_ip(host)
    if address is not None:
        return _classify_ip(address)
    if not host or not all(host.split(".")):
        return HostClass.PUBLIC
    if host.endswith(_TAILSCALE_SUFFIX) and len(host) > len(_TAILSCALE_SUFFIX):
        return HostClass.TAILSCALE
    if host.endswith(_LOCAL_SUFFIX) and len(host) > len(_LOCAL_SUFFIX):
        return HostClass.LAN
    return HostClass.PUBLIC


def check_runtime_url(url: object | None, *, allow_insecure_http: bool = False) -> Outcome:
    """Judge a Runtime Host address under the plaintext-http rule."""
    if not isinstance(url, str):
        return Outcome.INVALID
    text = url.strip()
    if not text:
        return Outcome.INVALID
    try:
        parsed = urlparse(text)
        host = parsed.hostname
    except ValueError:
        return Outcome.INVALID
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc or host is None:
        return Outcome.INVALID
    if scheme == "https":
        return Outcome.ALLOWED
    # A backslash is a path separator to some URL parsers and userinfo to
    # others, so two components could disagree about which host this is.
    if "\\" in text:
        return Outcome.INVALID
    host_class = classify_host(host)
    if host_class in (HostClass.LOOPBACK, HostClass.TAILSCALE):
        return Outcome.ALLOWED
    if host_class is HostClass.LAN:
        return Outcome.ALLOWED_WARN if allow_insecure_http else Outcome.REFUSED_LAN
    return Outcome.REFUSED_PUBLIC


def refusal_message(url: object | None, outcome: Outcome) -> str:
    """The error a refused address gets; a LAN refusal names the opt-in."""
    text = str(url).strip() if isinstance(url, str) else repr(url)
    if outcome is Outcome.REFUSED_LAN:
        return (
            f"Refusing plaintext {text}: http:// to a LAN address sends the device token and every transcript "
            f"unencrypted. If you trust this network, opt in with {OPT_IN_FLAG} (or {OPT_IN_ENV}=1). "
            "Otherwise use https://, or reach the box over Tailscale, where http:// is allowed "
            "(a 100.x address or a .ts.net name)."
        )
    if outcome is Outcome.REFUSED_PUBLIC:
        return (
            f"Refusing plaintext {text}: http:// is allowed only to loopback and Tailscale addresses "
            "(100.64.0.0/10, fd7a:115c:a1e0::/48, *.ts.net). Use https:// (Caddy, nginx, or `tailscale serve`)."
        )
    return f"{text!r} is not an http(s) Longhouse address."


def insecure_warning(url: str) -> str:
    """The one-line warning printed each time an opted-in LAN address is used."""
    return (
        f"WARNING: {url.strip()} is plain http, allowed by {OPT_IN_FLAG}: "
        "the device token and every transcript cross this network unencrypted."
    )


def env_opt_in() -> bool:
    return os.environ.get(OPT_IN_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
