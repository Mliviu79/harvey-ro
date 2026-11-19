"""Who may use the dashboard API (harvey/dashboard_auth.py).

Everything here runs against throwaway paths: the token, .env, database and
PID/log files are all redirected into tmp_path, and starting Harvey is
stubbed. Covers: every route needs the token (enumerated, so a new route
can't slip through), the public shell carries no data, Host/Origin/fetch
metadata checks, cross-site "simple" requests, token storage, .env
persistence, loopback-only binding, the opt-in origin a reverse proxy serves
the dashboard at, a real server started as the CLI does, and the browser-side
token handling under node.
"""

import errno
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

import harvey.dashboard as dash
from harvey import dashboard_auth as auth

# The settings tests save the email campaign keys, which Settings offers
# only while that module (the Email switch) is on.
pytestmark = pytest.mark.modules_on("email_outreach")

POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes (Windows uses ACLs)")
ORIGIN = "http://127.0.0.1:5555"
PUBLIC = "https://harvey.example.com"
CROSS_SITE = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site",
              "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "empty"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Every path the dashboard writes, redirected; Popen stubbed."""
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "harvey.db")
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "harvey.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "harvey.log")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    # Not set at all here (conftest sets it empty), so this .env is looked at.
    monkeypatch.delenv(auth.ORIGIN_ENV, raising=False)
    spawned = []

    class NoPopen:
        def __init__(self, *a, **k):
            spawned.append(a)
            raise AssertionError("Harvey must not be started by this request")

    monkeypatch.setattr(dash.subprocess, "Popen", NoPopen)
    return {"dir": tmp_path, "spawned": spawned, "token": dash.token_store.get()}


@pytest.fixture
def anon(env):
    with TestClient(dash.app, base_url=ORIGIN) as c:
        yield c


@pytest.fixture
def authed(env):
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        yield c


def _protected_requests():
    """(method, path) for every route except the static shell."""
    out = []
    for route in dash.app.routes:
        if not isinstance(route, Route):
            continue
        if route.path in ("/", "/static/{path:path}"):
            continue
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        for method in sorted(route.methods - {"HEAD"}):
            out.append((method, path))
    return out


PROTECTED = _protected_requests()


def test_the_route_list_is_not_empty():
    assert len(PROTECTED) >= 30
    assert ("POST", "/api/settings/env") in PROTECTED


# ── Every API route needs the token ──


@pytest.mark.parametrize("method, path", PROTECTED)
def test_every_route_refuses_requests_without_the_token(anon, env, method, path):
    r = anon.request(method, path, json={"SMTP_HOST": "relay.example.test"}
                     if method == "POST" else None)
    assert r.status_code == 401, (method, path, r.status_code)
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert "access-control-allow-origin" not in r.headers
    assert not env["spawned"]
    assert not dash.ENV_FILE.exists()


@pytest.mark.parametrize("method, path", PROTECTED)
def test_cross_site_requests_are_refused_even_with_the_token(env, method, path):
    with TestClient(dash.app, base_url=ORIGIN) as c:
        r = c.request(method, path, headers={**CROSS_SITE,
                                             "Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 403, (method, path)
    assert not env["spawned"] and not dash.ENV_FILE.exists()


def test_token_in_query_or_cookie_is_ignored(anon, env):
    tok = env["token"]
    for params in ({"token": tok}, {"t": tok}, {"access_token": tok}):
        assert anon.get("/api/companies", params=params).status_code == 401
    for cookie in (f"token={tok}", f"harvey_token={tok}", f"Authorization=Bearer {tok}"):
        assert anon.get("/api/companies", headers={"Cookie": cookie}).status_code == 401


@pytest.mark.parametrize("value", [
    "Basic {t}", "Bearer", "Bearer ", "Bearer wrong-token", "Bearer {t}x", "Bearer {tp}",
    "{t}", "Token {t}",
])
def test_malformed_or_wrong_bearer_is_refused(anon, env, value):
    tok = env["token"]
    header = value.format(t=tok, tp=tok[:-1])
    assert anon.get("/api/companies", headers={"Authorization": header}).status_code == 401


def test_bearer_scheme_is_case_insensitive(anon, env):
    r = anon.get("/api/companies", headers={"Authorization": f"bearer {env['token']}"})
    assert r.status_code == 200


def test_docs_and_openapi_are_gone(authed):
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert authed.get(path).status_code == 404


def test_no_cors_preflight_is_ever_approved(anon, authed):
    for c in (anon, authed):
        r = c.options("/api/settings/env", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type"})
        assert r.status_code in (401, 403, 405)
        assert not any(k.startswith("access-control-") for k in r.headers)


# ── The public shell ──


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/static/auth.js", "/static/app.css"])
def test_shell_is_public_but_carries_no_data_or_token(anon, env, path):
    r = anon.get(path)
    assert r.status_code == 200
    assert env["token"] not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp


def test_shell_has_no_token_bearing_links(anon):
    html = anon.get("/").text
    assert 'href="/api/' not in html
    assert "/static/auth.js" in html
    assert html.index("/static/auth.js") < html.index("/static/app.js")
    assert '<meta name="referrer" content="no-referrer">' in html


def test_only_get_and_head_of_the_shell_are_public(anon):
    assert anon.post("/").status_code == 401
    assert anon.post("/static/app.js").status_code == 401


# ── Host: loopback names only, checked before the token ──


@pytest.mark.parametrize("host", [
    "evil.example", "rebind.evil.example:5555", "127.0.0.1.nip.io", "localhost.",
    "localhost.evil.example", "127.0.0.1:5555@evil.example", "evil.example@127.0.0.1",
    "127.0.0.1:abc", "127.0.0.1:99999", "127.0.0.1:", "[::1", "::1", "[127.0.0.1]",
    "127.1", "0.0.0.0", "0x7f000001", "2130706433", "192.168.1.10", "localhost:5555:1",
    "localhost:5555 ", "local%68ost", "[::1]x", "[fe80::1%25lo0]:5555",
    # Zone IDs on loopback: ipaddress calls "::1%lo0" loopback, Harvey doesn't.
    "[::1%lo0]:5555", "[::1%lo0]", "[::1%25lo0]:5555", "[::1%1]", "[0:0:0:0:0:0:0:1%lo0]",
])
def test_non_loopback_or_malformed_hosts_are_refused(env, host):
    with TestClient(dash.app, base_url=ORIGIN) as c:
        for path, headers in (("/", {}), ("/api/companies",
                                          {"Authorization": f"Bearer {env['token']}"})):
            r = c.get(path, headers={**headers, "Host": host})
            assert r.status_code == 400, (host, path, r.status_code)
            assert env["token"] not in r.text


@pytest.mark.parametrize("name", ["::1%lo0", "::1%1", "::1%25lo0", "127.0.0.1%lo0"])
def test_zone_ids_are_never_loopback(name):
    import ipaddress
    if "::" in name:
        assert ipaddress.ip_address(name).is_loopback  # the trap being avoided
    assert not auth.is_loopback(name)
    assert auth.parse_host(f"[{name}]:5555") is None


@pytest.mark.parametrize("host", ["127.0.0.1:5555", "localhost:5555", "LOCALHOST:5555",
                                  "[::1]:5555", "127.0.0.1", "127.0.0.2:8080"])
def test_loopback_hosts_are_accepted(env, host):
    with TestClient(dash.app, base_url=ORIGIN) as c:
        r = c.get("/api/companies", headers={"Host": host,
                                             "Authorization": f"Bearer {env['token']}"})
        assert r.status_code == 200, host


async def _raw(headers, method="GET", path="/api/companies", body=b""):
    """Drive the ASGI app directly, for header shapes clients won't send."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "client": ("127.0.0.1", 1),
             "server": ("127.0.0.1", 5555),
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers]}
    await dash.app(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


@pytest.mark.asyncio
async def test_missing_or_repeated_host_and_auth_headers_are_refused(env):
    bearer = ("authorization", f"Bearer {env['token']}")
    assert await _raw([bearer]) == 400
    assert await _raw([("host", "127.0.0.1:5555"), ("host", "evil.example"), bearer]) == 400
    assert await _raw([("host", "evil.example"), ("host", "127.0.0.1:5555"), bearer]) == 400
    assert await _raw([("host", "127.0.0.1:5555"), bearer, bearer]) == 401
    assert await _raw([("host", "127.0.0.1:5555"), bearer]) == 200


@pytest.mark.asyncio
async def test_websockets_are_closed(env):
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    await dash.app({"type": "websocket", "path": "/api/companies", "headers": [
        (b"host", b"127.0.0.1:5555")], "query_string": b""}, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]


# ── Browser provenance ──


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
    {"Origin": "http://localhost:5555"},          # a different origin from 127.0.0.1
    {"Origin": "http://127.0.0.1:3000"},          # same site, other port
    {"Origin": "http://127.0.0.1:5555.evil.example"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"},
])
def test_foreign_browser_contexts_are_refused(authed, headers):
    assert authed.get("/api/companies", headers=headers).status_code == 403
    assert authed.post("/api/sending/pause", headers=headers).status_code == 403


def test_same_origin_browser_requests_work(authed, env):
    headers = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
    assert authed.get("/api/companies", headers=headers).status_code == 200
    r = authed.post("/api/settings/env", json={"SMTP_HOST": "smtp.example.test"},
                    headers=headers)
    assert r.status_code == 200 and r.json()["success"]
    assert "SMTP_HOST=smtp.example.test" in dash.ENV_FILE.read_text()


def test_top_level_navigation_to_the_shell_is_allowed_from_anywhere(anon):
    nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
           "Sec-Fetch-Dest": "document"}
    assert anon.get("/", headers=nav).status_code == 200
    # ...but not framing, subresources, or the API.
    assert anon.get("/", headers={**nav, "Sec-Fetch-Dest": "iframe"}).status_code == 403
    assert anon.get("/static/app.js", headers={**CROSS_SITE,
                                               "Sec-Fetch-Dest": "script"}).status_code == 403
    assert anon.get("/api/companies", headers=nav).status_code == 403


# ── Behind a reverse proxy: one https origin, opt-in ──


@pytest.fixture
def proxied(env):
    """The dashboard as a reverse proxy serves it at PUBLIC."""
    dash.public_origin.configure(PUBLIC)
    with TestClient(dash.app, base_url=PUBLIC,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        yield c


@pytest.mark.parametrize("value, origin", [
    ("https://harvey.example.com", PUBLIC), ("https://Harvey.Example.COM/", PUBLIC),
    ("  https://harvey.example.com:443 ", PUBLIC), ("HTTPS://harvey.example.com", PUBLIC),
    ("https://harvey.example.com:8443", "https://harvey.example.com:8443"),
    ("https://harvey.example.com:0443", PUBLIC),
    ("https://harvey.example.com:08443", "https://harvey.example.com:8443"),
    ("https://xn--harvey-9za.example", "https://xn--harvey-9za.example"),
    ("", None), ("   ", None), (None, None),
])
def test_public_origin_is_written_as_browsers_send_it(value, origin):
    assert auth.parse_public_origin(value) == origin


@pytest.mark.parametrize("value", [
    "http://harvey.example.com", "harvey.example.com", "//harvey.example.com",
    "https://harvey.example.com/dashboard", "https://harvey.example.com//",
    "https://harvey.example.com?x=1", "https://harvey.example.com#x",
    "https://user@harvey.example.com", "https://harvey.example.com.",
    "https://harvey", "https://localhost", "https://harvey.localhost", "https://127.0.0.1",
    "https://192.0.2.10", "https://[::1]", "https://[2001:db8::1]", "https://2130706433.1",
    "https://-harvey.example.com", "https://harvey_x.example.com", "https://*.example.com",
    "https://harvey.example.com:0", "https://harvey.example.com:", "https://harvey.example.com:99999",
    "https://harvey.example.com, https://other.example.com", "https://harvey.example.com other",
    "https://" + "a" * 64 + ".example.com", "https://harvey..example.com", "null", "*",
    "https://192.0.2.0x4", "https://harvey.0x7f", "https://harvey.example.1com", "https",
    "https://" + ".".join(["a" * 60] * 4) + ".example.com",       # over 253 characters
])
def test_unusable_public_origins_are_refused(value):
    with pytest.raises(auth.OriginError, match=auth.ORIGIN_ENV):
        auth.parse_public_origin(value)


def test_a_refused_value_leaves_loopback_only():
    public = auth.PublicOrigin()
    public.configure(PUBLIC)
    assert public.origin == PUBLIC and public.host == "harvey.example.com"
    with pytest.raises(auth.OriginError):
        public.configure("http://harvey.example.com")
    assert public.origin is None and public.host is None


def test_public_host_is_refused_until_configured(env):
    with TestClient(dash.app, base_url=PUBLIC,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        assert c.get("/").status_code == 400
        r = c.get("/api/companies", headers={"Origin": PUBLIC, "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 400 and r.json()["error"] == "bad_host"
        assert auth.ORIGIN_ENV not in r.text


def test_proxied_dashboard_works_from_its_own_origin(proxied, env):
    headers = {"Origin": PUBLIC, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
    assert proxied.get("/api/companies", headers=headers).status_code == 200
    r = proxied.post("/api/settings/env", json={"SMTP_HOST": "smtp.example.test"},
                     headers=headers)
    assert r.status_code == 200 and r.json()["success"]
    assert "SMTP_HOST=smtp.example.test" in dash.ENV_FILE.read_text()
    assert proxied.post("/api/sending/pause", headers=headers).status_code == 200
    # No Origin at all (curl, or a same-origin GET) still needs only the token.
    assert proxied.get("/api/companies").status_code == 200
    # The unlock link, clicked on another site.
    nav = {"Origin": "https://mail.example.net", "Sec-Fetch-Site": "cross-site",
           "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    assert proxied.get("/", headers={**nav, "Authorization": ""}).status_code == 200
    assert proxied.get("/api/companies", headers=nav).status_code == 403


def test_proxied_dashboard_still_needs_the_token(proxied, env):
    headers = {"Origin": PUBLIC, "Sec-Fetch-Site": "same-origin"}
    for auth_header in ("", "Bearer " + "x" * 43, f"Basic {env['token']}"):
        r = proxied.get("/api/companies", headers={**headers, "Authorization": auth_header})
        assert r.status_code == 401, auth_header
        r = proxied.post("/api/sending/pause", headers={**headers, "Authorization": auth_header})
        assert r.status_code == 401, auth_header
    r = proxied.get(f"/api/companies?token={env['token']}",
                    headers={"Authorization": "", "Cookie": f"token={env['token']}"})
    assert r.status_code == 401
    shell = proxied.get("/", headers={"Authorization": ""})
    assert shell.status_code == 200 and env["token"] not in shell.text


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
    {"Origin": "http://harvey.example.com"},            # the same name over plain http
    {"Origin": "https://harvey.example.com:8443"},
    {"Origin": "https://harvey.example.com:443"},       # browsers never write the default port
    {"Origin": "https://harvey.example.com."},
    {"Origin": "https://sub.harvey.example.com"},
    {"Origin": "https://harvey.example.com.evil.example"},
    {"Origin": "https://example.com"},
    {"Origin": ORIGIN},                                 # a loopback origin on the public host
    {"Origin": "http://harvey.example.com:5555"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},                    # a sibling subdomain's page
    {"Origin": PUBLIC, "Sec-Fetch-Site": "same-site"},
    {"Origin": PUBLIC, "Sec-Fetch-Site": "cross-site"},
])
def test_foreign_browser_contexts_are_refused_behind_the_proxy(proxied, headers):
    assert proxied.get("/api/companies", headers=headers).status_code == 403
    r = proxied.post("/api/sending/pause", headers=headers)
    assert r.status_code == 403
    assert r.json()["message"] in ("Cross-origin requests are refused.",
                                   "Cross-site requests are refused.")
    assert r.headers["strict-transport-security"] == "max-age=31536000"


@pytest.mark.asyncio
async def test_two_origin_headers_are_refused_behind_the_proxy(env):
    dash.public_origin.configure(PUBLIC)
    base = [("host", "harvey.example.com"), ("authorization", f"Bearer {env['token']}")]
    assert await _raw(base + [("origin", PUBLIC)]) == 200
    assert await _raw(base + [("origin", PUBLIC), ("origin", PUBLIC)]) == 403
    assert await _raw(base + [("origin", PUBLIC), ("origin", "https://evil.example")]) == 403
    assert await _raw(base + [("host", "127.0.0.1:5555")]) == 400


@pytest.mark.parametrize("host", [
    "harvey.example.com:443", "harvey.example.com:5555", "harvey.example.com:80",
    "harvey.example.com.", "sub.harvey.example.com", "example.com", "evil.example",
    "harvey.example.com.evil.example", "xharvey.example.com", "harvey.example.com@evil.example",
    "evil.example@harvey.example.com", "https://harvey.example.com", "harvey.example.com/",
])
def test_only_the_configured_public_host_is_answered(proxied, env, host):
    for path in ("/", "/api/companies"):
        r = proxied.get(path, headers={"Host": host})
        assert r.status_code == 400, (host, path, r.status_code)
        assert env["token"] not in r.text
        # Says where to look, without naming the address.
        assert auth.ORIGIN_ENV in r.json()["message"] and "harvey.example" not in r.text


def test_the_public_host_is_matched_whatever_its_case(proxied):
    assert proxied.get("/api/companies", headers={"Host": "Harvey.Example.COM"}).status_code == 200


def test_a_public_origin_with_a_port_names_that_port(env):
    dash.public_origin.configure("https://harvey.example.com:8443")
    token = {"Authorization": f"Bearer {env['token']}"}
    with TestClient(dash.app, base_url="https://harvey.example.com:8443", headers=token) as c:
        ok = {"Origin": "https://harvey.example.com:8443", "Sec-Fetch-Site": "same-origin"}
        assert c.post("/api/sending/pause", headers=ok).status_code == 200
        assert c.post("/api/sending/pause", headers={"Origin": PUBLIC}).status_code == 403
        assert c.get("/api/companies", headers={"Host": "harvey.example.com"}).status_code == 400
        # HSTS covers every port of a name, so none is sent for one port of it.
        assert "strict-transport-security" not in c.get("/").headers


def test_loopback_keeps_working_beside_the_public_origin(proxied, env):
    """`harvey dashboard --url` on the machine itself, or an SSH tunnel."""
    local = {"Host": "127.0.0.1:5555"}
    assert proxied.get("/api/companies", headers=local).status_code == 200
    same = {**local, "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
    assert proxied.post("/api/sending/pause", headers=same).status_code == 200


def test_a_proxy_that_replaces_host_is_refused_and_told_why(proxied, authed):
    """The page is at the public address, but the proxy sent Harvey its own
    loopback Host: refused as any other origin is, with the reason."""
    rewritten = {"Host": "127.0.0.1:5555", "Origin": PUBLIC, "Sec-Fetch-Site": "same-origin"}
    r = proxied.post("/api/sending/pause", headers=rewritten)
    assert r.status_code == 403 and r.json()["error"] == "cross_origin"
    assert "pass Host through" in r.json()["message"]
    assert "strict-transport-security" not in r.headers
    other = proxied.post("/api/sending/pause", headers={**rewritten, "Origin": "https://evil.example"})
    assert other.json()["message"] == "Cross-origin requests are refused."
    # With no public origin set, nothing hints at one.
    dash.public_origin.configure(None)
    r = authed.post("/api/sending/pause", headers={"Origin": PUBLIC})
    assert r.status_code == 403 and r.json()["message"] == "Cross-origin requests are refused."


def test_hsts_is_sent_for_the_public_host_only(proxied, env):
    def hsts(response):
        return response.headers.get("strict-transport-security")

    assert hsts(proxied.get("/")) == "max-age=31536000"
    assert hsts(proxied.get("/api/companies")) == "max-age=31536000"
    assert hsts(proxied.get("/api/companies", headers={"Authorization": ""})) == "max-age=31536000"
    assert hsts(proxied.get("/", headers={"Host": "127.0.0.1:5555"})) is None
    assert hsts(proxied.get("/", headers={"Host": "evil.example"})) is None
    for name, value in auth.SECURITY_HEADERS:
        assert proxied.get("/").headers[name.decode()] == value.decode()


def test_no_cors_preflight_is_approved_behind_the_proxy(proxied):
    r = proxied.options("/api/companies", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization"})
    assert r.status_code == 403
    own = proxied.options("/api/companies", headers={
        "Origin": PUBLIC, "Access-Control-Request-Method": "GET"})
    assert not any(h.lower().startswith("access-control-") for h in own.headers)


@pytest.mark.parametrize("content_type", [
    "text/plain;charset=UTF-8", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
])
def test_cross_site_simple_requests_change_nothing(env, content_type):
    """What a hostile page can send with fetch(..., {mode: 'no-cors'}) or a
    form: no custom headers, a CORS-safelisted content type."""
    body = json.dumps({"SMTP_HOST": "relay.example.test"})
    with TestClient(dash.app, base_url=ORIGIN) as c:
        for path in ("/api/settings/env", "/api/harvey/start", "/api/outbox/approve-all",
                     "/api/sending/resume", "/api/modules/web_marketing"):
            r = c.post(path, content=body, headers={**CROSS_SITE, "Content-Type": content_type})
            assert r.status_code == 403
            # Older browsers send no fetch metadata: the missing token still stops it.
            r = c.post(path, content=body, headers={"Content-Type": content_type})
            assert r.status_code == 401
    assert not dash.ENV_FILE.exists() and not env["spawned"]


@pytest.mark.parametrize("content_type", [
    "text/plain;charset=UTF-8", "application/x-www-form-urlencoded", "", "application/jsonx",
])
def test_authorised_mutations_with_a_body_must_be_json(authed, content_type):
    r = authed.post("/api/settings/env", content=b'{"SMTP_HOST": "x.example.test"}',
                    headers={"Content-Type": content_type})
    assert r.status_code == 415
    assert not dash.ENV_FILE.exists()


def test_bodyless_mutations_still_work(authed):
    assert authed.post("/api/sending/pause").json()["success"]
    assert authed.post("/api/sending/resume").json()["success"]


# ── Authorised flows ──


def test_authorised_export_downloads_csv(authed):
    r = authed.get("/api/export/prospects.csv", params={"all": "true"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"


def test_authorised_settings_round_trip(authed):
    r = authed.post("/api/settings/env", json={"SMTP_HOST": "smtp.example.test",
                                               "SMTP_PASSWORD": "pw-123", "NOT_ALLOWED": "x"})
    assert r.json()["success"]
    data = authed.get("/api/settings").json()
    assert data["smtp_host"] == "smtp.example.test" and data["smtp_password_set"] is True
    assert "pw-123" not in json.dumps(data)
    assert "NOT_ALLOWED" not in dash.ENV_FILE.read_text()


def test_imap_and_microsoft_settings_round_trip(authed):
    r = authed.post("/api/settings/env", json={
        "IMAP_HOST": "imap.example.test", "IMAP_USERNAME": "reader@example.test",
        "IMAP_PASSWORD": "imap-pw-9", "M365_CLIENT_ID": "client-1", "M365_TENANT_ID": "tenant-2"})
    assert r.json()["success"]
    data = authed.get("/api/settings").json()
    assert data["imap_host"] == "imap.example.test" and data["imap_username"] == "reader@example.test"
    assert data["imap_password_set"] is True and "imap-pw-9" not in json.dumps(data)
    assert (data["m365_client_id"], data["m365_tenant_id"]) == ("client-1", "tenant-2")
    assert data["m365_authorized"] in (True, False)


def test_a_port_harvey_cannot_parse_is_refused(authed):
    r = authed.post("/api/settings/env", json={"IMAP_PORT": "993 SSL"})
    assert r.status_code == 400 and not dash.ENV_FILE.exists()
    assert authed.post("/api/settings/env", json={"IMAP_PORT": ""}).json()["success"]   # blank = default
    assert authed.post("/api/settings/env", json={"SMTP_PORT": " 587 "}).json()["success"]


@pytest.mark.parametrize("value", [{"a": 1}, ["x"], True, "x" * 5000, "a\x00b"])
def test_settings_values_are_validated(authed, value):
    r = authed.post("/api/settings/env", json={"SMTP_HOST": value})
    assert r.status_code == 400
    assert not dash.ENV_FILE.exists()


# ── .env persistence ──


@POSIX
def test_new_env_file_is_owner_only_whatever_the_umask(authed):
    old = os.umask(0)
    try:
        assert authed.post("/api/settings/env", json={"SMTP_PASSWORD": "s3cret"}).json()["success"]
    finally:
        os.umask(old)
    assert stat.S_IMODE(dash.ENV_FILE.stat().st_mode) == 0o600


@POSIX
def test_existing_env_file_is_tightened_and_its_other_lines_kept(authed, env):
    original = ("# Harvey settings\n"
                "INSTANTLY_API_KEY=keep-me\n"
                "\n"
                "export SMTP_HOST=old.example.test\n"
                "CUSTOM_THING=value # with comment\n"
                "SMTP_HOST=duplicate.example.test\n")
    dash.ENV_FILE.write_text(original)
    os.chmod(dash.ENV_FILE, 0o644)
    assert authed.post("/api/settings/env",
                       json={"SMTP_HOST": "new.example.test", "SMTP_PORT": 587}).json()["success"]
    text = dash.ENV_FILE.read_text()
    assert text == ("# Harvey settings\n"
                    "INSTANTLY_API_KEY=keep-me\n"
                    "\n"
                    "SMTP_HOST=new.example.test\n"
                    "CUSTOM_THING=value # with comment\n"
                    "SMTP_PORT=587\n")
    assert stat.S_IMODE(dash.ENV_FILE.stat().st_mode) == 0o600
    assert [p.name for p in env["dir"].iterdir() if p.name.startswith(".env.")] == []


@POSIX
def test_symlinked_env_is_not_followed_or_replaced(authed, env):
    target = env["dir"] / "elsewhere.env"
    target.write_text("SMTP_HOST=untouched\n")
    dash.ENV_FILE.symlink_to(target)
    r = authed.post("/api/settings/env", json={"SMTP_HOST": "x.example.test"})
    assert r.json()["success"] is False
    assert dash.ENV_FILE.is_symlink()
    assert target.read_text() == "SMTP_HOST=untouched\n"


def test_failed_env_write_leaves_the_original_intact(authed, env, monkeypatch):
    dash.ENV_FILE.write_text("SMTP_HOST=before\n")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(dash.os, "replace", boom)
    r = authed.post("/api/settings/env", json={"SMTP_HOST": "after"})
    assert r.json()["success"] is False
    assert dash.ENV_FILE.read_text() == "SMTP_HOST=before\n"
    assert [p.name for p in env["dir"].iterdir() if p.name.startswith(".env.")] == []


# ── The token file ──


@POSIX
def test_token_is_created_owner_only_in_a_private_data_dir(tmp_path):
    path = tmp_path / "fresh" / "data" / "dashboard-token"
    old = os.umask(0)
    try:
        token = auth.load_or_create_token(path)
    finally:
        os.umask(old)
    assert auth.TOKEN_RE.fullmatch(token)
    assert len(token) >= 43  # 256 bits
    st = path.stat()
    assert stat.S_IMODE(st.st_mode) == 0o600 and st.st_nlink == 1
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.read_text().strip() == token
    assert [p.name for p in path.parent.iterdir()] == ["dashboard-token"]


def test_token_is_stable_and_unique_per_install(tmp_path):
    a = auth.load_or_create_token(tmp_path / "a" / "t")
    assert auth.load_or_create_token(tmp_path / "a" / "t") == a
    assert auth.load_or_create_token(tmp_path / "b" / "t") != a


@pytest.mark.parametrize("round_number", range(10))
def test_concurrent_creation_yields_one_token(tmp_path, round_number):
    path = tmp_path / "data" / "dashboard-token"
    results, errors = [], []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        try:
            results.append(auth.load_or_create_token(path))
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)
    assert not errors and len(results) == 8 and len(set(results)) == 1
    assert [p.name for p in path.parent.iterdir()] == ["dashboard-token"]


@pytest.mark.parametrize("error_code, winerror", [
    (errno.EACCES, None), (errno.EINVAL, 5), (errno.EINVAL, 32), (errno.EINVAL, 33),
])
def test_windows_transient_token_open_recovers_without_replacing(tmp_path, monkeypatch, error_code, winerror):
    path = tmp_path / "dashboard-token"
    original = auth.load_or_create_token(path)
    before = path.read_bytes()
    real_open = auth.os.open
    calls, sleeps = [], []

    def open_busy(name, flags, *args, **kwargs):
        if Path(name) == path:
            calls.append(name)
            if len(calls) <= 2:
                error = OSError(error_code, "Permission denied")
                if winerror is not None:
                    error.winerror = winerror
                raise error
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(auth, "_IS_WINDOWS", True)
    monkeypatch.setattr(auth.os, "open", open_busy)
    monkeypatch.setattr(auth.time, "sleep", sleeps.append)
    monkeypatch.setattr(auth, "_publish_token", lambda *a: pytest.fail("existing token must not be replaced"))
    assert auth.load_or_create_token(path) == original
    assert len(calls) == 3 and sleeps == [0.01, 0.01]
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("windows, expected_attempts", [(True, 51), (False, 1)])
def test_token_open_denied_fails_closed_with_bounded_retry(tmp_path, monkeypatch, windows, expected_attempts):
    path = tmp_path / "dashboard-token"
    auth.load_or_create_token(path)
    before = path.read_bytes()
    attempts, sleeps = [], []
    denied = PermissionError(errno.EACCES, "Permission denied")

    def open_denied(*args, **kwargs):
        attempts.append(1)
        raise denied

    monkeypatch.setattr(auth, "_IS_WINDOWS", windows)
    monkeypatch.setattr(auth.os, "open", open_denied)
    monkeypatch.setattr(auth.time, "sleep", sleeps.append)
    monkeypatch.setattr(auth, "_publish_token", lambda *a: pytest.fail("denial must not create a token"))
    with pytest.raises(auth.TokenError, match="Can't read") as caught:
        auth.load_or_create_token(path)
    assert caught.value.__cause__ is denied
    assert len(attempts) == expected_attempts
    assert sleeps == [0.01] * (expected_attempts - 1)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_windows_unrelated_open_error_is_not_retried(tmp_path, monkeypatch):
    path = tmp_path / "dashboard-token"
    auth.load_or_create_token(path)
    before = path.read_bytes()
    error = OSError(errno.EIO, "I/O error")

    def fail_open(*args, **kwargs):
        raise error

    monkeypatch.setattr(auth, "_IS_WINDOWS", True)
    monkeypatch.setattr(auth.os, "open", fail_open)
    monkeypatch.setattr(auth.time, "sleep", lambda *a: pytest.fail("I/O failure must fail immediately"))
    monkeypatch.setattr(auth, "_publish_token", lambda *a: pytest.fail("read errors must not replace the token"))
    with pytest.raises(auth.TokenError, match="Can't read") as caught:
        auth.load_or_create_token(path)
    assert caught.value.__cause__ is error
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["malformed", "symlink", "security"])
def test_windows_validation_errors_are_not_retried(tmp_path, monkeypatch, failure):
    path = tmp_path / "dashboard-token"
    auth.load_or_create_token(path)
    if failure == "malformed":
        path.write_text("invalid")
    before = path.read_bytes()
    monkeypatch.setattr(auth, "_IS_WINDOWS", True)
    monkeypatch.setattr(auth.time, "sleep", lambda *a: pytest.fail("validation must fail immediately"))
    monkeypatch.setattr(auth, "_publish_token", lambda *a: pytest.fail("invalid token must not be replaced"))
    if failure == "symlink":
        monkeypatch.setattr(auth.os.path, "islink", lambda p: True)
    elif failure == "security":
        def reject(*args):
            raise auth.TokenError("not a regular file")
        monkeypatch.setattr(auth, "_check_open_file", reject)
    with pytest.raises(auth.TokenError):
        auth.load_or_create_token(path)
    assert path.read_bytes() == before


def _expect_locked(path, match):
    with pytest.raises(auth.TokenError, match=match):
        auth.load_or_create_token(path)


@POSIX
def test_readable_existing_token_is_refused(tmp_path):
    path = tmp_path / "dashboard-token"
    path.write_text("A" * 43)
    os.chmod(path, 0o644)
    _expect_locked(path, "readable or writable by other users")
    assert path.read_text() == "A" * 43  # left for the operator to inspect


@POSIX
def test_symlinked_token_is_refused(tmp_path):
    real = tmp_path / "real-token"
    real.write_text("B" * 43)
    os.chmod(real, 0o600)
    path = tmp_path / "dashboard-token"
    path.symlink_to(real)
    _expect_locked(path, "symbolic link")


@POSIX
def test_dangling_symlink_is_not_followed_to_create_a_file(tmp_path):
    path = tmp_path / "dashboard-token"
    target = tmp_path / "planted-target"
    path.symlink_to(target)
    _expect_locked(path, "symbolic link")
    assert not target.exists()


@POSIX
def test_hardlinked_token_is_refused(tmp_path):
    path = tmp_path / "dashboard-token"
    path.write_text("C" * 43)
    os.chmod(path, 0o600)
    os.link(path, tmp_path / "copy")
    _expect_locked(path, "hard links")


@pytest.mark.parametrize("content", [b"", b"short", b"x" * 43 + b"!", b"\xff" * 50,
                                     b"D" * 5000])
def test_malformed_token_is_refused(tmp_path, content):
    path = tmp_path / "dashboard-token"
    path.write_bytes(content)
    if os.name != "nt":
        os.chmod(path, 0o600)
    _expect_locked(path, "valid token")


@POSIX
def test_non_regular_token_paths_are_refused(tmp_path):
    d = tmp_path / "dir-token"
    d.mkdir()
    _expect_locked(d, "not a regular file|Can't read")
    fifo = tmp_path / "fifo-token"
    os.mkfifo(fifo, 0o600)
    _expect_locked(fifo, "not a regular file")  # and doesn't hang


@POSIX
def test_unusable_token_locks_the_api_but_not_the_shell(env, anon):
    path = env["dir"] / "data" / "locked-token"
    path.write_text("E" * 43)
    os.chmod(path, 0o640)
    dash.token_store.configure(path)
    r = anon.get("/api/companies", headers={"Authorization": "Bearer " + "E" * 43})
    assert r.status_code == 503 and "Remove it" in r.json()["message"]
    assert anon.get("/").status_code == 200


# ── Binding and startup ──

def _never_serve(monkeypatch):
    """The dashboard's server (dash._server_class, which start_dashboard
    runs) fails the test if it is ever started."""
    class Server:
        def __init__(self, config):
            pytest.fail("must not start")
    monkeypatch.setattr(dash, "_server_class", lambda: Server)



@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com", "",
                                  "127.0.0.1.nip.io", "localhost.evil.example"])
def test_non_loopback_binds_are_refused(env, monkeypatch, host):
    _never_serve(monkeypatch)
    with pytest.raises(dash.DashboardSetupError, match="only listens on this machine"):
        dash.start_dashboard(host=host, port=5555)


@pytest.mark.parametrize("host, shown", [("127.0.0.1", "127.0.0.1"), ("localhost", "localhost"),
                                         ("::1", "[::1]"), ("[::1]", "[::1]")])
def test_loopback_bind_prints_a_fragment_link(env, monkeypatch, capsys, host, shown):
    calls = []

    class Server:
        def __init__(self, config):
            calls.append(config)

        def run(self):
            pass
    monkeypatch.setattr(dash, "_server_class", lambda: Server)
    dash.start_dashboard(host=host, port=5599)
    out = capsys.readouterr().out
    assert f"http://{shown}:5599/#token={env['token']}" in out
    assert "?token" not in out and "?t=" not in out
    assert calls[0].host == host.strip("[]") and calls[0].proxy_headers is False
    assert calls[0].log_level == "warning"


def _serve_nothing(monkeypatch):
    calls = []

    class Server:
        def __init__(self, config):
            calls.append(config)

        def run(self):
            pass
    monkeypatch.setattr(dash, "_server_class", lambda: Server)
    return calls


def test_public_origin_from_the_environment_gives_its_link(env, monkeypatch, capsys):
    calls = _serve_nothing(monkeypatch)
    monkeypatch.setenv(auth.ORIGIN_ENV, "https://Harvey.Example.com/")
    dash.start_dashboard(port=5599)
    out = capsys.readouterr().out
    assert f"{PUBLIC}/#token={env['token']}" in out and "http://127.0.0.1" not in out
    assert f"Served at {PUBLIC} by your reverse proxy" in out
    assert "listens only on 127.0.0.1 port 5599" in out
    assert f"reach {PUBLIC}: limit who can at the proxy" in " ".join(out.split())
    assert "?token" not in out
    # Still bound to loopback, still blind to forwarded headers.
    assert calls[0].host == "127.0.0.1" and calls[0].proxy_headers is False
    assert dash.public_origin.origin == PUBLIC and dash.public_origin.host == "harvey.example.com"


def test_public_origin_is_read_from_the_env_file_and_the_environment_wins(env, monkeypatch):
    dash.ENV_FILE.write_text(f"SMTP_HOST=smtp.example.test\n{auth.ORIGIN_ENV}={PUBLIC}\n")
    assert dash.dashboard_url() == f"{PUBLIC}/#token={env['token']}"
    assert auth.ORIGIN_ENV not in os.environ           # .env is read, not loaded
    monkeypatch.setenv(auth.ORIGIN_ENV, "https://other.example.com")
    assert dash.dashboard_url() == f"https://other.example.com/#token={env['token']}"
    monkeypatch.setenv(auth.ORIGIN_ENV, "")            # set but empty: loopback only
    assert dash.dashboard_url() == f"http://127.0.0.1:5555/#token={env['token']}"
    assert dash.public_origin.origin is None           # a link asked for changes nothing


@pytest.mark.parametrize("line", [
    'export HARVEY_DASHBOARD_ORIGIN="https://harvey.example.com"  # behind the proxy',
    "HARVEY_DASHBOARD_ORIGIN='https://harvey.example.com/'\r",
    "\ufeffHARVEY_DASHBOARD_ORIGIN=https://harvey.example.com",
])
def test_public_origin_is_read_however_the_env_file_spells_it(env, line):
    dash.ENV_FILE.write_text(line + "\n", encoding="utf-8", newline="")
    assert dash.dashboard_url() == f"{PUBLIC}/#token={env['token']}"


def test_an_env_file_that_cannot_be_read_as_utf8_still_starts(env, monkeypatch):
    _serve_nothing(monkeypatch)
    dash.ENV_FILE.write_bytes(b"# caf\xe9\nSMTP_HOST=smtp.example.test\n")
    assert dash.dashboard_url() == f"http://127.0.0.1:5555/#token={env['token']}"
    dash.start_dashboard()
    assert dash.public_origin.origin is None
    dash.ENV_FILE.unlink()
    dash.ENV_FILE.mkdir()                              # can't be read at all
    assert dash.dashboard_url() == f"http://127.0.0.1:5555/#token={env['token']}"


def test_public_origin_is_taken_at_start_not_when_saved(authed, env):
    """Saving .env from the dashboard never changes who may reach it."""
    dash.ENV_FILE.write_text(f"{auth.ORIGIN_ENV}={PUBLIC}\n")
    assert authed.post("/api/settings/env", json={"SMTP_HOST": "smtp.example.test"}).status_code == 200
    assert os.environ[auth.ORIGIN_ENV] == PUBLIC       # the save loads .env; the guard doesn't look
    assert dash.public_origin.origin is None
    with TestClient(dash.app, base_url=PUBLIC,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        assert c.get("/api/companies").status_code == 400


@pytest.mark.parametrize("value", ["http://harvey.example.com", "harvey.example.com",
                                   "https://203.0.113.7", "https://harvey.example.com/x"])
def test_an_unusable_public_origin_stops_the_start(env, monkeypatch, capsys, value):
    import harvey.cli as cli
    _never_serve(monkeypatch)
    monkeypatch.setenv(auth.ORIGIN_ENV, value)
    with pytest.raises(dash.DashboardSetupError, match=auth.ORIGIN_ENV):
        dash.start_dashboard()
    for url in (False, True):
        args = type("A", (), {"host": "127.0.0.1", "port": 5555, "url": url})()
        with pytest.raises(SystemExit) as e:
            cli.cmd_dashboard(args)
        assert e.value.code == 2
        out = capsys.readouterr().out
        assert "Dashboard not started" in out and env["token"] not in out
    assert dash.public_origin.origin is None


def test_public_origin_never_widens_the_bind(env, monkeypatch):
    _never_serve(monkeypatch)
    monkeypatch.setenv(auth.ORIGIN_ENV, PUBLIC)
    for host in ("0.0.0.0", "harvey.example.com", "::"):
        with pytest.raises(dash.DashboardSetupError, match="only listens on this machine"):
            dash.start_dashboard(host=host)


def test_cli_url_prints_the_public_link(env, monkeypatch, capsys):
    import harvey.cli as cli
    _never_serve(monkeypatch)
    monkeypatch.setenv(auth.ORIGIN_ENV, PUBLIC)
    cli.cmd_dashboard(type("A", (), {"host": "127.0.0.1", "port": 5555, "url": True})())
    assert capsys.readouterr().out.strip() == f"{PUBLIC}/#token={env['token']}"


def test_startup_fails_closed_without_a_usable_token(env, monkeypatch):
    _never_serve(monkeypatch)
    bad = env["dir"] / "data" / "bad-token"
    bad.write_text("nope")
    if os.name != "nt":
        os.chmod(bad, 0o600)
    dash.token_store.configure(bad)
    with pytest.raises(dash.DashboardSetupError, match="valid token"):
        dash.start_dashboard()


def test_cli_dashboard_refuses_and_prints_links(env, monkeypatch, capsys):
    import harvey.cli as cli
    _never_serve(monkeypatch)
    args = type("A", (), {"host": "0.0.0.0", "port": 5555, "url": False})()
    with pytest.raises(SystemExit) as e:
        cli.cmd_dashboard(args)
    assert e.value.code == 2
    assert "Dashboard not started" in capsys.readouterr().out
    args = type("A", (), {"host": "127.0.0.1", "port": 5555, "url": True})()
    cli.cmd_dashboard(args)
    assert capsys.readouterr().out.strip() == f"http://127.0.0.1:5555/#token={env['token']}"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


START = """
import sys
from pathlib import Path
import harvey.dashboard as dash
work = Path(sys.argv[1])
dash.ENV_FILE = work / ".env"
dash.DB_PATH = work / "data" / "harvey.db"
dash.PID_FILE = work / "data" / "harvey.pid"
dash.LOG_FILE = work / "data" / "harvey.log"
dash.token_store.configure(work / "data" / "dashboard-token")
from harvey.cli import main
sys.argv = ["harvey", "dashboard", "--port", sys.argv[2]]
main()
"""


def test_real_server_started_like_the_cli(tmp_path):
    """uvicorn on a real loopback socket, started through `harvey dashboard`."""
    port = _free_port()
    script = tmp_path / "start.py"
    script.write_text(START)
    root = Path(dash.__file__).resolve().parent.parent
    proc = subprocess.Popen([sys.executable, "-u", str(script), str(tmp_path), str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**os.environ, "PYTHONPATH": str(root)})
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(200):
            try:
                httpx.get(base + "/", timeout=0.5)
                break
            except httpx.TransportError:
                time.sleep(0.05)
        token = (tmp_path / "data" / "dashboard-token").read_text().strip()
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as c:
            assert c.get("/").status_code == 200
            assert token not in c.get("/").text
            assert c.get("/api/companies").status_code == 401
            assert c.get(f"/api/companies?token={token}").status_code == 401
            ok = {"Authorization": f"Bearer {token}"}
            assert c.get("/api/companies", headers=ok).status_code == 200
            assert c.get("/api/companies", headers={**ok, "Host": "evil.example"}).status_code == 400
            r = c.post("/api/settings/env", content='{"SMTP_HOST":"x.example.test"}',
                       headers={**CROSS_SITE, "Content-Type": "text/plain"})
            assert r.status_code == 403
            # A key of a module that is on in a new install (email_verify).
            r = c.post("/api/settings/env", json={"REOON_API_KEY": "fictional-key"}, headers=ok)
            assert r.json()["success"]
            assert "server" not in r.headers
    finally:
        proc.terminate()
        out, _ = proc.communicate(timeout=10)
    assert f"http://127.0.0.1:{port}/#token={token}" in out
    assert "GET /api" not in out  # no access log lines
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "data" / "dashboard-token").stat().st_mode) == 0o600


# ── The browser side (auth.js / app.js) under node ──

WEB = Path(dash.__file__).resolve().parent / "web"

NODE_HARNESS = r"""
const store = {};
const calls = [];
let replaced = null;
globalThis.window = {location: {origin: 'http://127.0.0.1:5555', pathname: '/',
                                search: '', hash: process.argv[2] || ''}};
globalThis.location = window.location;
globalThis.history = {replaceState: (s, t, url) => { replaced = url; window.location.hash = ''; }};
globalThis.sessionStorage = {getItem: k => (k in store ? store[k] : null),
                             setItem: (k, v) => { store[k] = String(v); },
                             removeItem: k => { delete store[k]; }};
globalThis.fetch = (url, opts) => { calls.push({url, opts}); return Promise.resolve({}); };
eval(require('fs').readFileSync(process.argv[1], 'utf8') +
     ';globalThis.T = {authFetch, sameOriginUrl, authToken, setAuthToken};');
(async () => {
  const out = {replaced, stored: authToken(), hashAfter: window.location.hash};
  out.urls = ['/api/x', '//evil.example/api', 'https://evil.example/api', '/\\evil.example',
              'javascript:alert(1)', 'api/x', '/api/x?all=true']
    .map(p => { const u = T.sameOriginUrl(p); return u ? u.href : null; });
  out.offOrigin = await T.authFetch('https://evil.example/api').then(() => 'sent', () => 'refused');
  await T.authFetch('/api/companies', {method: 'POST', headers: {'Content-Type': 'application/json'}});
  const c = calls[calls.length - 1];
  out.call = {url: c.url, auth: c.opts.headers.get('authorization'),
              ctype: c.opts.headers.get('content-type'), mode: c.opts.mode,
              credentials: c.opts.credentials, redirect: c.opts.redirect,
              referrerPolicy: c.opts.referrerPolicy};
  out.calls = calls.length;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node(hash_value):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", NODE_HARNESS, str(WEB / "auth.js"), hash_value],
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_fragment_token_moves_to_session_storage_and_leaves_the_url():
    token = "T" * 43
    out = _node(f"#token={token}")
    assert out["stored"] == token
    assert out["replaced"] == "/" and out["hashAfter"] == ""
    assert out["call"]["auth"] == f"Bearer {token}"
    assert out["call"]["url"] == "http://127.0.0.1:5555/api/companies"
    assert out["call"]["ctype"] == "application/json"
    assert out["call"]["mode"] == "same-origin" and out["call"]["credentials"] == "omit"
    assert out["call"]["redirect"] == "error" and out["call"]["referrerPolicy"] == "same-origin"


def test_bad_fragment_is_stripped_but_not_stored():
    out = _node("#token=<script>")
    assert out["stored"] == "" and out["replaced"] == "/"
    assert out["call"]["auth"] is None


def test_token_is_never_sent_off_origin():
    out = _node("#token=" + "T" * 43)
    assert out["urls"] == ["http://127.0.0.1:5555/api/x", None, None, None, None, None,
                           "http://127.0.0.1:5555/api/x?all=true"]
    assert out["offOrigin"] == "refused"
    assert out["calls"] == 1  # only the same-origin call went out


def test_collected_links_cannot_run_script_in_the_dashboard():
    """Websites and profile URLs come from scraped data; only http(s) links
    are rendered, so a javascript: URL can't reach the token."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    src = (WEB / "app.js").read_text(encoding="utf-8")
    funcs = "".join(re.search(rf"^function {n}\(.*?^}}\n", src, re.S | re.M).group(0)
                    for n in ("escHtml", "safeHref"))
    urls = ["https://ok.example/a?b=1&c=2", "javascript:alert(1)", " JaVaScRiPt:x",
            "data:text/html,x", "//evil.example", "vbscript:x"]
    script = funcs + f"process.stdout.write(JSON.stringify({json.dumps(urls)}.map(safeHref)))"
    out = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out == ["https://ok.example/a?b=1&amp;c=2", "", "", "", "", ""]
    assert 'href="\' + escHtml(' not in src


def test_app_js_only_talks_to_the_api_through_authfetch():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert re.findall(r"(?<![A-Za-z])fetch\(", app) == []
    assert "authFetch(" in app
    assert "localStorage.setItem('harvey-dashboard" not in app
    # The contact CSVs download through downloadCsv (authFetch + a blob).
    assert "downloadCsv('/api/export/prospects.csv" in app
