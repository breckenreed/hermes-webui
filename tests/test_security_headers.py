"""Security headers and the CSP nonce handshake.

The value of the policy rests on one thing: the nonce in the header matches
the nonce on the page's <script> tags, and nothing else can satisfy it. Both
halves are asserted here, because getting one right alone either breaks the
app (page inert) or silently gives up the protection ('unsafe-inline').
"""
import re

import pytest

import server

NONCE_RE = re.compile(r"script-src [^;]*'nonce-([A-Za-z0-9_-]+)'")


def _csp(response):
    return response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("header,value", [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
])
def test_static_headers_present(client, header, value):
    assert client.get("/").headers[header] == value


def test_permissions_policy_denies_device_access(client):
    policy = client.get("/").headers["Permissions-Policy"]
    for feature in ("geolocation", "microphone", "camera"):
        assert f"{feature}=()" in policy


class TestContentSecurityPolicy:
    def test_script_src_has_no_unsafe_inline(self, client):
        """The whole point of the nonce: 'unsafe-inline' would re-enable every
        injected <script> and onerror= handler the policy exists to stop."""
        script_src = next(
            part.strip() for part in _csp(client.get("/")).split(";")
            if part.strip().startswith("script-src ")
        )
        assert "'unsafe-inline'" not in script_src
        assert "'nonce-" in script_src

    def test_locks_down_the_dangerous_directives(self, client):
        policy = _csp(client.get("/"))
        for directive in ("object-src 'none'", "frame-ancestors 'none'",
                          "base-uri 'none'", "form-action 'none'",
                          "connect-src 'self'"):
            assert directive in policy

    def test_nonce_is_fresh_per_response(self, client):
        first = NONCE_RE.search(_csp(client.get("/"))).group(1)
        second = NONCE_RE.search(_csp(client.get("/"))).group(1)
        assert first != second

    def test_page_scripts_carry_the_matching_nonce(self, client):
        response = client.get("/")
        nonce = NONCE_RE.search(_csp(response)).group(1)
        body = response.text

        tags = re.findall(r"<script\b[^>]*>", body)
        assert tags, "index.html has no <script> tags — check the fixture"
        for tag in tags:
            assert f'nonce="{nonce}"' in tag, f"un-nonced script tag: {tag}"

    def test_no_stale_nonce_from_a_previous_response(self, client):
        """The page is cached in memory between requests; the nonce must not
        be cached along with it."""
        first = client.get("/")
        second = client.get("/")
        first_nonce = NONCE_RE.search(_csp(first)).group(1)
        assert first_nonce not in second.text


class TestApiResponses:
    def test_api_is_not_cacheable(self, client, turns_dir):
        assert "no-store" in client.get("/api/turns").headers["Cache-Control"]

    def test_headers_survive_an_auth_rejection(self, client, token, turns_dir):
        """security_headers is registered outermost so a 401 is covered too —
        an error page is still a page an injection could land on."""
        response = client.get("/api/turns")
        assert response.status_code == 401
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" in response.headers


class TestStrictTransportSecurity:
    def test_absent_over_plain_http(self, client, monkeypatch):
        monkeypatch.delenv("WEBUI_TLS", raising=False)
        assert "Strict-Transport-Security" not in client.get("/").headers

    def test_present_when_tls_is_on(self, client, monkeypatch):
        """Only sent under TLS: pinning HTTPS for a host that then serves
        plain HTTP would lock the user out of their own webui."""
        monkeypatch.setenv("WEBUI_TLS", "1")
        header = client.get("/").headers["Strict-Transport-Security"]
        assert "max-age=" in header


class TestMarkdownEscaping:
    """The CSP is the second line of defence; index.html's escaping is the
    first. This guards the regression that motivated the nonce."""

    def test_link_url_cannot_break_out_of_the_href_attribute(self):
        source = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        # The old pattern was (https?:[^)]+) — a URL could contain a quote and
        # close the attribute, injecting an event handler into the tag.
        assert "(https?:[^)]+)" not in source
        assert r"""(https?:[^)\s"'`<>]+)""" in source
