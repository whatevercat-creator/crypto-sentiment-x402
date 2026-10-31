"""
Sending alerts to addresses customers choose, without letting them point this
server at itself or its private network (SSRF).

Checked twice. validate_target runs when a watch is created and again before
every send, so rows stored before these rules existed are covered too.
post_json then resolves DNS, refuses unless every address is public, and
connects to the address it checked, so a hostname can't pass the check and
then change its answer ("DNS rebinding"). The original hostname still goes in
the Host header and TLS, so certificates are verified against it.

Results are the generic strings below and nothing else. Exception text can
carry the target URL, and for Telegram the URL holds the bot token.
"""

import asyncio
import ipaddress
import logging
import socket
from typing import Optional

import httpx

MAX_TARGET_LENGTH = 2048
TOTAL_TIMEOUT_SECONDS = 10
STEP_TIMEOUT_SECONDS = 5

ERROR_TIMEOUT = "delivery failed: timeout"
ERROR_CONNECTION = "delivery failed: connection error"
ERROR_NOT_ALLOWED = "delivery failed: target not allowed"
ERROR_NOT_CONFIGURED = "delivery failed: not configured"

# app/main.py logs at INFO, and httpx logs every request URL at INFO: that
# would put the Telegram bot token in the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa")
# Public-looking, but reach an embedded IPv4 address or a cloud host's own
# services: NAT64, 6to4, Teredo, documentation, Azure's metadata address.
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "2001:db8::/32", "168.63.129.16/32")
)


class TargetNotAllowed(ValueError):
    """The message is fixed text, safe to show the customer."""


def is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if any(ip.version == net.version and ip in net for net in _BLOCKED_NETWORKS):
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


def _literal_ip(host: str) -> Optional[str]:
    """The address if host is an IP, including the short forms the resolver
    accepts (2130706433, 0x7f.1, 127.1)."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except OSError:
        return None


def _check_host(host: str) -> None:
    host = host.lower().rstrip(".")
    ip = _literal_ip(host)
    if ip is not None:
        if not is_public_ip(ip):
            raise TargetNotAllowed("channel_target can't be a private or local address.")
        return
    if "." not in host or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise TargetNotAllowed("channel_target can't be a private or local address.")


def _check_url(target: str) -> httpx.URL:
    if any(c <= " " or c in "\\\x7f" for c in target):
        raise TargetNotAllowed("channel_target must be a valid https URL.")
    try:
        url = httpx.URL(target)
        host = url.raw_host.decode("ascii")
    except (httpx.InvalidURL, UnicodeError):
        raise TargetNotAllowed("channel_target must be a valid https URL.")
    if url.scheme != "https" or not host or "%" in host or url.userinfo:
        raise TargetNotAllowed("channel_target must be a valid https URL.")
    _check_host(host)
    return url


def validate_target(channel_type: str, target: str) -> Optional[httpx.URL]:
    """The URL to send to (None for telegram, whose target is a chat ID), or
    TargetNotAllowed. Does no network calls."""
    if not target or len(target) > MAX_TARGET_LENGTH:
        raise TargetNotAllowed(f"channel_target must be 1 to {MAX_TARGET_LENGTH} characters.")
    if channel_type == "telegram":
        return None
    url = _check_url(target)
    if channel_type == "discord" and not (
        url.host == "discord.com"
        and url.port is None
        and url.path.startswith("/api/webhooks/")
        and len(url.path) > len("/api/webhooks/")
        and ".." not in url.path
    ):
        raise TargetNotAllowed("A Discord target must be a https://discord.com/api/webhooks/ URL.")
    return url


async def _resolve_public(host: str, port: int) -> list:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or not all(is_public_ip(a) for a in addresses):
        raise TargetNotAllowed("resolves to a non-public address")
    return sorted(addresses, key=lambda a: ":" in a)  # IPv4 first


async def _send_pinned(url: httpx.URL, body: dict) -> tuple[bool, str]:
    host = url.raw_host.decode("ascii")
    addresses = await _resolve_public(host, url.port or 443)
    timeout = httpx.Timeout(STEP_TIMEOUT_SECONDS)
    # trust_env=False: a proxy from the environment would re-resolve the name.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as http:
        for address in addresses:
            request = http.build_request(
                "POST",
                url.copy_with(host=address),
                json=body,
                headers={"Host": url.netloc.decode("ascii")},
                extensions={"sni_hostname": host},
            )
            try:
                # stream=True and no read: only the status matters, so the
                # response body never comes into memory, whatever its size.
                response = await http.send(request, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                continue
            status = response.status_code
            await response.aclose()
            if 200 <= status < 300:
                return True, ""
            return False, f"delivery failed: HTTP {status}"
    return False, ERROR_CONNECTION


async def post_json(url: httpx.URL, body: dict) -> tuple[bool, str]:
    """POST once to a URL validate_target accepted. Returns (delivered, error)."""
    try:
        return await asyncio.wait_for(_send_pinned(url, body), TOTAL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return False, ERROR_TIMEOUT
    except TargetNotAllowed:
        return False, ERROR_NOT_ALLOWED
    except Exception:
        return False, ERROR_CONNECTION
