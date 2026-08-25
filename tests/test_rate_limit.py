"""Per-identity turn limits.

Off by default — on a single-user localhost install this should not exist. It
earns its place once more than one person shares an inference host, because
agent mode auto-continues on its own and a runaway turn takes the model server
away from everybody else.
"""
import pytest

import server


@pytest.fixture(autouse=True)
def clean_counters(monkeypatch):
    monkeypatch.setattr(server, "_RATE_STARTS", {})
    monkeypatch.setattr(server, "_RATE_LIVE", {})
    monkeypatch.setattr(server, "RATE_LIMIT_TURNS", 0)
    monkeypatch.setattr(server, "RATE_LIMIT_CONCURRENT", 0)
    monkeypatch.setattr(server, "RATE_LIMIT_WINDOW", 3600.0)


class TestDisabledByDefault:
    def test_no_limit_means_no_refusal(self):
        for _ in range(50):
            server._rate_started("someone", "s")
        assert server._rate_check("someone") is None

    def test_a_normal_turn_is_unaffected(self, client, fake_stream):
        response = client.post("/api/chat",
                               json={"message": "hi", "session": "c_abc__1"})
        assert response.status_code == 200


class TestTurnsPerWindow:
    def test_refuses_past_the_ceiling(self, monkeypatch):
        monkeypatch.setattr(server, "RATE_LIMIT_TURNS", 2)
        server._rate_started("me", "a")
        server._rate_started("me", "b")
        refusal = server._rate_check("me")
        assert refusal is not None

    def test_the_refusal_says_what_the_limit_is_and_when_it_frees_up(self, monkeypatch):
        """A bare refusal in the transcript is indistinguishable from the agent
        being broken."""
        monkeypatch.setattr(server, "RATE_LIMIT_TURNS", 1)
        monkeypatch.setattr(server, "RATE_LIMIT_WINDOW", 600.0)
        server._rate_started("me", "a")
        refusal = server._rate_check("me")
        assert "1" in refusal["error"] and "min" in refusal["error"]
        assert refusal["retry_after"] > 0

    def test_old_starts_fall_out_of_the_window(self, monkeypatch):
        monkeypatch.setattr(server, "RATE_LIMIT_TURNS", 1)
        monkeypatch.setattr(server, "RATE_LIMIT_WINDOW", 0.0001)
        server._rate_started("me", "a")
        import time
        time.sleep(0.01)
        assert server._rate_check("me") is None

    def test_one_identity_does_not_spend_anothers_budget(self, monkeypatch):
        monkeypatch.setattr(server, "RATE_LIMIT_TURNS", 1)
        server._rate_started("me", "a")
        assert server._rate_check("me") is not None
        assert server._rate_check("someone-else") is None


class TestConcurrency:
    def test_refuses_a_second_simultaneous_turn(self, monkeypatch):
        monkeypatch.setattr(server, "RATE_LIMIT_CONCURRENT", 1)
        server._rate_started("me", "a")
        assert server._rate_check("me") is not None

    def test_a_finished_turn_releases_its_slot(self, monkeypatch):
        monkeypatch.setattr(server, "RATE_LIMIT_CONCURRENT", 1)
        server._rate_started("me", "a")
        server._rate_finished("me", "a")
        assert server._rate_check("me") is None

    def test_the_refusal_points_at_stopping_one(self, monkeypatch):
        monkeypatch.setattr(server, "RATE_LIMIT_CONCURRENT", 1)
        server._rate_started("me", "a")
        assert "stop" in server._rate_check("me")["error"].lower()


class TestThroughTheEndpoint:
    def test_a_refused_turn_is_a_429_and_records_nothing(
            self, client, monkeypatch, turns_dir):
        """The check runs before the record is created and before the process
        is spawned, so a refusal leaves nothing to recover or clean up."""
        monkeypatch.setattr(server, "RATE_LIMIT_TURNS", 1)
        server._rate_started("ip:testclient", "earlier")
        response = client.post("/api/chat",
                               json={"message": "hi", "session": "c_abc__1"})
        assert response.status_code == 429
        assert "error" in response.json()
        assert server.TURNS == {}
        assert list(turns_dir.iterdir()) == []

    def test_identity_falls_back_to_the_client_address(self, client, monkeypatch):
        """A token identifies a person only once tokens are issued per person."""
        monkeypatch.setattr(server, "WEBUI_TOKEN", "")
        monkeypatch.setattr(server, "RATE_LIMIT_TURNS", 1)
        server._rate_started("ip:testclient", "earlier")
        assert client.post("/api/chat",
                           json={"message": "hi", "session": "c_abc__2"}).status_code == 429

    def test_distinct_tokens_are_distinct_identities(self, client, monkeypatch, token):
        monkeypatch.setattr(server, "RATE_LIMIT_TURNS", 1)
        mine = "t:" + server._token_digest(token)
        server._rate_started(mine, "earlier")
        assert client.post("/api/chat", headers={"Authorization": f"Bearer {token}"},
                           json={"message": "hi", "session": "c_abc__3"}).status_code == 429
