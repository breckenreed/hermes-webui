"""Access control around WEBUI_TOKEN.

/api/turns is the probe endpoint throughout: it is the only /api/ route that
touches nothing but the filesystem, so these tests exercise the middleware
without needing docker or the agent container.
"""
import server


def test_open_when_no_token_configured(client, monkeypatch, turns_dir):
    monkeypatch.setattr(server, "WEBUI_TOKEN", "")
    assert client.get("/api/turns").status_code == 200


def test_missing_header_is_rejected(client, token, turns_dir):
    response = client.get("/api/turns")
    assert response.status_code == 401
    # Tells a client what scheme to use instead of failing blankly.
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_wrong_token_is_rejected(client, token, turns_dir):
    response = client.get("/api/turns", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_correct_token_is_accepted(client, auth, turns_dir):
    assert client.get("/api/turns", headers=auth).status_code == 200


def test_bare_token_without_bearer_prefix_is_accepted(client, token, turns_dir):
    """Documented leniency: the prefix is stripped when present, and the whole
    header is compared when it is not. A curl without `Bearer ` still works."""
    assert client.get("/api/turns",
                      headers={"Authorization": token}).status_code == 200


def test_partial_token_is_rejected(client, token, turns_dir):
    """A prefix of the real token must not compare equal."""
    response = client.get("/api/turns",
                          headers={"Authorization": f"Bearer {token[:-1]}"})
    assert response.status_code == 401


def test_page_shell_stays_public(client, token):
    """The lock screen has to be reachable to enter a token at all."""
    assert client.get("/").status_code == 200


def test_non_ascii_token_gives_401_not_500(client, token, turns_dir):
    """Regression: hmac.compare_digest() raises TypeError on non-ASCII str.

    Starlette decodes header bytes as latin-1, so any byte above 0x7f lands in
    the string as a character compare_digest refuses. A failed login used to
    escape the middleware as an unhandled exception and surface as a 500 with
    a traceback; it must be an ordinary 401.
    """
    raw = "Bearer parolüchik".encode("latin-1")
    response = client.get("/api/turns", headers={"Authorization": raw})
    assert response.status_code == 401


def test_lockout_after_repeated_failures(client, token, monkeypatch, turns_dir):
    monkeypatch.setattr(server, "AUTH_MAX_FAILURES", 3)
    monkeypatch.setattr(server, "AUTH_LOCKOUT_SECONDS", 60.0)

    # Distinct guesses — a repeat of the same wrong value is deliberately free
    # (see test_a_stale_token_does_not_lock_the_user_out).
    for i in range(3):
        assert client.get("/api/turns",
                          headers={"Authorization": f"Bearer bad{i}"}).status_code == 401

    locked = client.get("/api/turns", headers={"Authorization": "Bearer bad9"})
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0

    # The lockout is unconditional: the right token does not get through it
    # either, which is what stops it being a probe for a correct guess.
    assert client.get("/api/turns",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 429


def test_lockout_expires(client, token, monkeypatch, turns_dir):
    monkeypatch.setattr(server, "AUTH_MAX_FAILURES", 2)
    monkeypatch.setattr(server, "AUTH_LOCKOUT_SECONDS", 0.0)

    for i in range(2):
        client.get("/api/turns", headers={"Authorization": f"Bearer bad{i}"})
    # A zero-second lockout has already elapsed by the next request.
    assert client.get("/api/turns",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_success_clears_the_failure_streak(client, token, monkeypatch, turns_dir):
    monkeypatch.setattr(server, "AUTH_MAX_FAILURES", 3)

    client.get("/api/turns", headers={"Authorization": "Bearer bad0"})
    client.get("/api/turns", headers={"Authorization": "Bearer bad1"})
    assert client.get("/api/turns",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 200

    # Two more failures would trip the limit if the streak had carried over.
    for i in range(2, 4):
        assert client.get("/api/turns",
                          headers={"Authorization": f"Bearer bad{i}"}).status_code == 401


def test_a_stale_token_does_not_lock_the_user_out(client, token, monkeypatch,
                                                  turns_dir):
    """The page fires ~6 parallel /api/ calls on load. With a stale token in
    localStorage that must cost one failure, not six — otherwise rotating
    WEBUI_TOKEN locks the owner out of their own webui, unlock request
    included, and there is no way back in from the browser."""
    monkeypatch.setattr(server, "AUTH_MAX_FAILURES", 3)

    for _ in range(20):
        assert client.get("/api/turns",
                          headers={"Authorization": "Bearer stale"}).status_code == 401

    # Still reachable with the right token — never locked.
    assert client.get("/api/turns",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_distinct_guesses_still_trip_the_lockout(client, token, monkeypatch,
                                                 turns_dir):
    """The repeat exemption must not weaken brute-force protection: a real
    guessing run never sends the same wrong token twice."""
    monkeypatch.setattr(server, "AUTH_MAX_FAILURES", 3)
    monkeypatch.setattr(server, "AUTH_LOCKOUT_SECONDS", 60.0)

    for i in range(3):
        assert client.get("/api/turns",
                          headers={"Authorization": f"Bearer guess{i}"}).status_code == 401

    assert client.get("/api/turns",
                      headers={"Authorization": "Bearer guess9"}).status_code == 429


def test_alternating_guesses_still_count(client, token, monkeypatch, turns_dir):
    """Only an immediate repeat is free — alternating between two values must
    not halve the effective rate."""
    monkeypatch.setattr(server, "AUTH_MAX_FAILURES", 4)
    monkeypatch.setattr(server, "AUTH_LOCKOUT_SECONDS", 60.0)

    for value in ("a", "b", "a", "b"):
        client.get("/api/turns", headers={"Authorization": f"Bearer {value}"})

    assert client.get("/api/turns",
                      headers={"Authorization": "Bearer c"}).status_code == 429


def test_failure_table_is_bounded(monkeypatch):
    """A spoofed-source flood must not grow the table without limit."""
    monkeypatch.setattr(server, "_AUTH_FAILURES", {})
    monkeypatch.setattr(server, "_AUTH_TABLE_MAX", 8)
    for i in range(50):
        server._auth_record_failure(f"10.0.0.{i}")
    assert len(server._AUTH_FAILURES) <= 8


def test_forwarded_for_is_not_trusted(client, token, monkeypatch, turns_dir):
    """Honouring X-Forwarded-For would let one client mint an identity per
    guess and walk straight through the lockout."""
    monkeypatch.setattr(server, "AUTH_MAX_FAILURES", 2)
    monkeypatch.setattr(server, "AUTH_LOCKOUT_SECONDS", 60.0)

    for i in range(2):
        client.get("/api/turns", headers={"Authorization": f"Bearer bad{i}",
                                          "X-Forwarded-For": f"1.2.3.{i}"})
    response = client.get("/api/turns",
                          headers={"Authorization": "Bearer bad9",
                                   "X-Forwarded-For": "9.9.9.9"})
    assert response.status_code == 429


def test_token_compare_never_raises():
    assert server._token_ok("\udcff\udcfe") is False


class TestConfigAudit:
    def test_warns_when_token_absent(self, monkeypatch):
        monkeypatch.setattr(server, "WEBUI_TOKEN", "")
        warnings = server._audit_token_config()
        assert any("WEBUI_TOKEN is not set" in w for w in warnings)

    def test_warns_when_token_too_short(self, monkeypatch):
        monkeypatch.setattr(server, "WEBUI_TOKEN", "hunter2")
        monkeypatch.delenv("WEBUI_TLS", raising=False)
        warnings = server._audit_token_config()
        assert any("only 7 characters" in w for w in warnings)

    def test_warns_when_token_travels_in_cleartext(self, monkeypatch):
        monkeypatch.setattr(server, "WEBUI_TOKEN", "x" * 32)
        monkeypatch.delenv("WEBUI_TLS", raising=False)
        assert any("WEBUI_TLS is off" in w for w in server._audit_token_config())

    def test_quiet_when_properly_configured(self, monkeypatch):
        monkeypatch.setattr(server, "WEBUI_TOKEN", "x" * 32)
        monkeypatch.setenv("WEBUI_TLS", "1")
        assert server._audit_token_config() == []
