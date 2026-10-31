"""
app/outbound.py: what alert targets are accepted, and what a real send does.
The send tests run a local TLS server, so pinning, SNI and certificate
checks are exercised on real connections.
"""

import asyncio
import datetime
import functools
import http.server
import json
import logging
import socketserver
import ssl
import threading
import time

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import tests.test_home  # noqa: F401
from app import alerts, outbound
from app.outbound import TargetNotAllowed, is_public_ip, validate_target

HOST = "hook.customer.test"


@pytest.mark.parametrize("target", [
    "https://hooks.zapier.com/hooks/catch/1/abc/",
    "https://webhook.site/abc?x=1",
    "https://api.customer.dev:8443/hook",
    "https://8.8.8.8/hook",
    "https://[2606:4700:4700::1111]/hook",
])
def test_public_https_targets_accepted(target):
    assert validate_target("webhook", target) is not None


@pytest.mark.parametrize("target", [
    "http://hooks.zapier.com/x",
    "ftp://hooks.zapier.com/x",
    "file:///etc/passwd",
    "hooks.zapier.com/x",
    "https://",
    "https://localhost/x",
    "https://LOCALHOST/x",
    "https://localhost./x",
    "https://foo.localhost/x",
    "https://intranet/x",
    "https://metadata/x",
    "https://metadata.google.internal/computeMetadata/v1/",
    "https://printer.local/x",
    "https://127.0.0.1/x",
    "https://127.1/x",
    "https://2130706433/x",
    "https://0x7f.0.0.1/x",
    "https://0177.0.0.1/x",
    "https://0.0.0.0/x",
    "https://10.0.0.1/x",
    "https://172.16.0.1/x",
    "https://192.168.1.1/x",
    "https://100.64.0.1/x",
    "https://100.100.100.200/x",
    "https://169.254.169.254/latest/meta-data/",
    "https://168.63.129.16/x",
    "https://224.0.0.1/x",
    "https://240.0.0.1/x",
    "https://[::1]/x",
    "https://[::]/x",
    "https://[::ffff:127.0.0.1]/x",
    "https://[::ffff:169.254.169.254]/x",
    "https://[::127.0.0.1]/x",
    "https://[fc00::1]/x",
    "https://[fd00:ec2::254]/x",
    "https://[fe80::1]/x",
    "https://[ff02::1]/x",
    "https://[64:ff9b::7f00:1]/x",
    "https://[2002:7f00:1::]/x",
    "https://user:pass@hooks.zapier.com/x",
    "https://hooks.zapier.com\\@127.0.0.1/x",
    "https://hooks.zapier.com /x",
    "https://hooks.zapier.com/\nx",
    "https://hooks.zapier.com/" + "a" * 2048,
    "",
])
def test_bad_targets_refused(target):
    with pytest.raises(TargetNotAllowed):
        validate_target("webhook", target)


def test_length_limit_is_2048():
    base = "https://hooks.zapier.com/"
    assert validate_target("webhook", base + "a" * (2048 - len(base)))
    with pytest.raises(TargetNotAllowed):
        validate_target("telegram", "1" * 2049)


def test_refusal_never_echoes_the_target():
    with pytest.raises(TargetNotAllowed) as e:
        validate_target("webhook", "https://10.0.0.1/tok3n")
    assert "tok3n" not in str(e.value)


def test_discord_target_accepted():
    assert validate_target("discord", "https://discord.com/api/webhooks/1/abc")


@pytest.mark.parametrize("target", [
    "https://evil.dev/api/webhooks/1/abc",
    "https://discord.com.evil.dev/api/webhooks/1/abc",
    "https://discord.com/api/webhooks/",
    "https://discord.com/api/users/1",
    "https://discord.com/api/webhooks/../users/1",
    "https://discord.com/api/webhooks/%2e%2e/users/1",
    "https://discord.com:8443/api/webhooks/1/abc",
    "http://discord.com/api/webhooks/1/abc",
])
def test_discord_rejects_everything_else(target):
    with pytest.raises(TargetNotAllowed):
        validate_target("discord", target)


@pytest.mark.parametrize("address,public", [
    ("8.8.8.8", True), ("2606:4700:4700::1111", True), ("127.0.0.1", False),
    ("169.254.169.254", False), ("::1", False), ("fd00::1", False), ("::ffff:10.0.0.1", False),
])
def test_is_public_ip(address, public):
    assert is_public_ip(address) is public


def _fake_dns(monkeypatch, addresses):
    async def getaddrinfo(self, host, port, **kwargs):
        return [(2, 1, 6, "", (a, port)) for a in addresses]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["169.254.169.254"], ["::1"], ["fd00::1"], [],
    ["8.8.8.8", "10.0.0.1"],  # one bad answer among good ones
])
def test_send_refused_when_dns_gives_non_public_address(monkeypatch, addresses):
    _fake_dns(monkeypatch, addresses)
    connected = []
    monkeypatch.setattr(outbound.httpx.AsyncClient, "send", lambda *a, **k: connected.append(a))
    result = asyncio.run(outbound.post_json(httpx.URL("https://rebind.customer.dev/x"), {}))
    assert result == (False, "delivery failed: target not allowed")
    assert connected == []


def test_public_answers_listed_ipv4_first(monkeypatch):
    _fake_dns(monkeypatch, ["2606:4700:4700::1111", "8.8.8.8"])
    assert asyncio.run(outbound._resolve_public("x.dev", 443)) == ["8.8.8.8", "2606:4700:4700::1111"]


# --- real TLS server ------------------------------------------------------


def _make_cert(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = tmp_path / "cert.pem"
    key_pem = tmp_path / "key.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    return pem, key_pem


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        server = self.server
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        server.seen.append({"path": self.path, "host": self.headers["host"], "body": body})
        behavior = server.behavior
        try:
            if behavior == "redirect":
                self.send_response(302)
                self.send_header("Location", f"https://{HOST}:{server.server_port}/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif behavior == "huge":
                self.send_response(200)
                self.send_header("Content-Length", str(10 ** 10))
                self.end_headers()
                while True:
                    self.wfile.write(b"x" * 65536)
            elif behavior == "hang":
                time.sleep(3)
            else:
                self.send_response(behavior)
                self.send_header("Content-Length", "0")
                self.end_headers()
        except OSError:
            pass  # the client hung up, as intended

    def log_message(self, *args):
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):  # skips HTTPServer's slow reverse DNS lookup
        socketserver.TCPServer.server_bind(self)
        self.server_port = self.server_address[1]


@pytest.fixture
def tls_server(monkeypatch, tmp_path):
    cert, key = _make_cert(tmp_path)
    server = _Server(("127.0.0.1", 0), _Handler)
    server.seen, server.sni, server.behavior = [], [], 204
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    context.sni_callback = lambda sock, name, ctx: server.sni.append(name)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()

    async def resolve(host, port):
        return ["127.0.0.1"]  # where the guard would have pinned it

    monkeypatch.setattr(outbound, "_resolve_public", resolve)
    trust = ssl.create_default_context(cafile=str(cert))
    monkeypatch.setattr(
        outbound.httpx, "AsyncClient", functools.partial(httpx.AsyncClient, verify=trust)
    )
    yield server
    server.shutdown()
    server.server_close()


def _url(server, path="/hook", host=HOST):
    return httpx.URL(f"https://{host}:{server.server_port}{path}")


def test_connects_to_checked_ip_with_hostname_in_host_sni_and_cert(tls_server):
    """HOST doesn't exist in DNS and the certificate names only HOST, not
    127.0.0.1, so this passes only if the connection went to the pinned
    address and TLS was checked against the hostname."""
    assert asyncio.run(outbound.post_json(_url(tls_server), {"a": 1})) == (True, "")
    seen = tls_server.seen[0]
    assert seen["path"] == "/hook"
    assert seen["host"] == f"{HOST}:{tls_server.server_port}"
    assert json.loads(seen["body"]) == {"a": 1}
    assert tls_server.sni == [HOST]


def test_certificate_for_another_name_fails(tls_server):
    result = asyncio.run(outbound.post_json(_url(tls_server, host="other.customer.test"), {}))
    assert result == (False, "delivery failed: connection error")
    assert tls_server.seen == []


def test_redirects_are_not_followed(tls_server):
    tls_server.behavior = "redirect"
    assert asyncio.run(outbound.post_json(_url(tls_server), {})) == (False, "delivery failed: HTTP 302")
    assert len(tls_server.seen) == 1


@pytest.mark.parametrize("status", [404, 500])
def test_error_status_is_generic(tls_server, status):
    tls_server.behavior = status
    assert asyncio.run(outbound.post_json(_url(tls_server), {})) == (False, f"delivery failed: HTTP {status}")


def test_endless_response_body_is_not_read(tls_server):
    tls_server.behavior = "huge"
    started = time.monotonic()
    assert asyncio.run(outbound.post_json(_url(tls_server), {})) == (True, "")
    assert time.monotonic() - started < 2


def test_total_timeout(tls_server, monkeypatch):
    tls_server.behavior = "hang"
    monkeypatch.setattr(outbound, "TOTAL_TIMEOUT_SECONDS", 0.3)
    assert asyncio.run(outbound.post_json(_url(tls_server), {})) == (False, "delivery failed: timeout")


def test_proxy_env_is_ignored(tls_server, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    assert asyncio.run(outbound.post_json(_url(tls_server), {})) == (True, "")


def test_token_stays_out_of_logs_and_errors(tls_server, caplog):
    url = _url(tls_server, path="/bot123:SECRETTOKEN/sendMessage")
    with caplog.at_level(logging.DEBUG):
        results = [asyncio.run(outbound.post_json(url, {}))]
        tls_server.behavior = 404
        results.append(asyncio.run(outbound.post_json(url, {})))
    assert "SECRETTOKEN" not in caplog.text
    assert "SECRETTOKEN" not in str(results)
    assert logging.getLogger("httpx").level == logging.WARNING


def test_connection_refused_error_is_generic(monkeypatch):
    async def resolve(host, port):
        return ["127.0.0.1"]

    monkeypatch.setattr(outbound, "_resolve_public", resolve)
    url = httpx.URL("https://closed.customer.test:9/bot123:SECRETTOKEN/x")
    assert asyncio.run(outbound.post_json(url, {})) == (False, "delivery failed: connection error")


def test_deliver_telegram_goes_through_post_json_and_hides_exceptions(monkeypatch):
    monkeypatch.setattr(alerts, "TELEGRAM_BOT_TOKEN", "123:SECRETTOKEN")
    sent = []

    async def post(url, body):
        sent.append((str(url), body))
        raise RuntimeError(f"boom at {url}")

    monkeypatch.setattr(alerts, "post_json", post)
    delivered, error = asyncio.run(
        alerts._deliver({"channel_type": "telegram", "channel_target": "42"}, "hello", {})
    )
    assert sent == [("https://api.telegram.org/bot123:SECRETTOKEN/sendMessage", {"chat_id": "42", "text": "hello"})]
    assert (delivered, error) == (False, "delivery failed: connection error")


def test_deliver_telegram_without_token(monkeypatch):
    monkeypatch.setattr(alerts, "TELEGRAM_BOT_TOKEN", "")
    result = asyncio.run(alerts._deliver({"channel_type": "telegram", "channel_target": "42"}, "m", {}))
    assert result == (False, "delivery failed: not configured")


@pytest.mark.parametrize("channel,target", [
    ("webhook", "http://169.254.169.254/latest/meta-data/"),
    ("webhook", "https://localhost/x"),
    ("discord", "https://evil.dev/api/webhooks/1/abc"),
])
def test_stored_bad_targets_are_refused_at_send_time(monkeypatch, channel, target):
    async def post(url, body):
        raise AssertionError("must not send")

    monkeypatch.setattr(alerts, "post_json", post)
    result = asyncio.run(alerts._deliver({"channel_type": channel, "channel_target": target}, "m", {}))
    assert result == (False, "delivery failed: target not allowed")


def test_deliver_payload_shapes(monkeypatch):
    sent = []

    async def post(url, body):
        sent.append(body)
        return True, ""

    monkeypatch.setattr(alerts, "post_json", post)
    asyncio.run(alerts._deliver({"channel_type": "webhook", "channel_target": "https://a.dev/x"}, "m", {"e": 1}))
    asyncio.run(alerts._deliver(
        {"channel_type": "discord", "channel_target": "https://discord.com/api/webhooks/1/a"}, "m", {"e": 1}
    ))
    assert sent == [{"e": 1}, {"content": "m"}]
