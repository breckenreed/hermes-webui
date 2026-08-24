"""Endpoints, with the agent container mocked away.

Nothing here shells out. Each test substitutes the one helper the endpoint
uses to reach docker, so the assertions are about how CLI output becomes JSON
— which is where the bugs actually live.
"""
import json

import pytest

import server


class FakeProc:
    """Stands in for the asyncio subprocess the docker helpers spawn."""

    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self._stdout, self._stderr = stdout, stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode


@pytest.fixture
def fake_exec(monkeypatch):
    """Replace subprocess spawning; the test supplies the output."""
    def install(stdout=b"", stderr=b"", returncode=0):
        async def _spawn(*args, **kwargs):
            install.calls.append(args)
            return FakeProc(stdout, stderr, returncode)
        monkeypatch.setattr(server.asyncio, "create_subprocess_exec", _spawn)
    install.calls = []
    return install


@pytest.fixture
def fake_run(monkeypatch):
    """Replace server._run, which returns (returncode, text)."""
    def install(text, code=0):
        async def _run(*args, **kwargs):
            install.calls.append(args)
            return code, text
        monkeypatch.setattr(server, "_run", _run)
    install.calls = []
    return install


@pytest.fixture(autouse=True)
def clear_caches(monkeypatch):
    """Endpoint caches are module globals; a stale one silently passes a test
    that never ran the code it claims to cover."""
    for name in ("MODELS_CACHE", "SKILLS_CACHE", "CONTEXT_CACHE", "MCP_CACHE"):
        monkeypatch.setattr(server, name, {"ts": 0.0, "data": None})
    monkeypatch.setattr(server, "AGENT_PROBE_CACHE",
                        {"ts": 0.0, "ready": False, "detail": "", "started_at": ""})


class TestHealth:
    """`/api/health` answers "can the agent take a turn?", not "is the
    container running?". The two came apart in practice: the agent exited, its
    restart policy revived it, every in-flight turn died — and a check that
    read only {{.State.Running}} stayed green throughout.
    """

    UP = b"true|2026-08-22T10:00:00Z|0|0|false\n"
    DOWN = b"false|2026-08-22T10:00:00Z|2|137|true\n"

    def test_green_needs_both_the_container_and_the_cli(
            self, client, fake_exec, fake_run):
        fake_exec(stdout=self.UP)
        fake_run("hermes 1.4.2")
        body = client.get("/api/health").json()
        assert body["state"] == "ok"
        assert body["ok"] is True
        assert body["agent_ready"] is True
        assert body["container"] == server.HERMES_CONTAINER

    def test_container_up_but_agent_silent_is_degraded(
            self, client, fake_exec, fake_run):
        """The case the old check called green — and the reason turns failed
        while the light said everything was fine."""
        fake_exec(stdout=self.UP)
        fake_run("bash: hermes: command not found", code=127)
        body = client.get("/api/health").json()
        assert body["state"] == "degraded"
        assert body["ok"] is False

    def test_stopped_container_is_offline(self, client, fake_exec, fake_run):
        fake_exec(stdout=self.DOWN)
        body = client.get("/api/health").json()
        assert body["state"] == "offline"
        assert body["ok"] is False
        # No point probing a CLI in a container that is not running.
        assert fake_run.calls == []

    def test_post_mortem_details_are_surfaced(self, client, fake_exec, fake_run):
        """Why it died is the difference between "restart it" and "give it
        more memory"."""
        fake_exec(stdout=self.DOWN)
        body = client.get("/api/health").json()
        assert body["restart_count"] == 2
        assert body["last_exit_code"] == 137
        assert body["oom_killed"] is True

    def test_started_at_is_reported_for_restart_detection(
            self, client, fake_exec, fake_run):
        """The client diffs this to notice a restart within one poll instead
        of waiting out the 45s stream watchdog."""
        fake_exec(stdout=self.UP)
        fake_run("hermes 1.4.2")
        assert client.get("/api/health").json()["started_at"] == "2026-08-22T10:00:00Z"

    def test_the_cli_probe_is_cached(self, client, fake_exec, fake_run):
        """It spawns a process; the UI polls health every 15s."""
        fake_exec(stdout=self.UP)
        fake_run("hermes 1.4.2")
        client.get("/api/health")
        client.get("/api/health")
        assert len(fake_run.calls) == 1

    def test_a_restart_invalidates_the_probe_cache(self, client, fake_exec,
                                                   fake_run):
        """Noticing a restart is the whole point, so the probe must never
        answer from state that predates one."""
        fake_exec(stdout=self.UP)
        fake_run("hermes 1.4.2")
        client.get("/api/health")

        fake_exec(stdout=b"true|2026-08-22T11:30:00Z|1|0|false\n")
        client.get("/api/health")
        assert len(fake_run.calls) == 2

    def test_a_docker_failure_is_not_a_500(self, client, monkeypatch):
        async def _boom(*args, **kwargs):
            raise OSError("docker socket missing")
        monkeypatch.setattr(server.asyncio, "create_subprocess_exec", _boom)

        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["state"] == "offline"
        assert "docker socket missing" in body["running"]

    def test_unparseable_inspect_output_is_offline(self, client, fake_exec,
                                                   fake_run):
        fake_exec(stdout=b"Error: No such container: hermes-agent\n")
        body = client.get("/api/health").json()
        assert body["state"] == "offline"
        assert "No such container" in body["running"]


class TestSessionsList:
    LISTING = (
        "  Preview                 Workspace   Last Active   Src\n"
        "  Fix the build           /work       3m ago        cli   20260715_193102_62eba9\n"
        "  —                       /work       just now      cli   20260716_101500_aa11bb\n"
    ).encode()

    def test_parses_the_table(self, client, fake_exec):
        fake_exec(stdout=self.LISTING)
        sessions = client.get("/api/sessions").json()["sessions"]
        assert [s["id"] for s in sessions] == [
            "20260715_193102_62eba9", "20260716_101500_aa11bb"]
        assert sessions[0]["title"] == "Fix the build"
        assert sessions[0]["last_active"] == "3m ago"

    def test_placeholder_titles_become_empty(self, client, fake_exec):
        fake_exec(stdout=self.LISTING)
        assert client.get("/api/sessions").json()["sessions"][1]["title"] == ""

    def test_ansi_escapes_are_stripped(self, client, fake_exec):
        fake_exec(stdout=b"  \x1b[1mBold title\x1b[0m   /w   1m ago   cli   "
                         b"20260715_193102_62eba9\n")
        title = client.get("/api/sessions").json()["sessions"][0]["title"]
        assert "\x1b" not in title
        assert title == "Bold title"

    def test_the_injected_preamble_never_reaches_the_sidebar(self, client, fake_exec):
        preview = f"{server.PREAMBLE_OPEN}note{server.PREAMBLE_CLOSE} real title"
        fake_exec(stdout=f"  {preview}   /w   1m ago   cli   "
                         "20260715_193102_62eba9\n".encode())
        title = client.get("/api/sessions").json()["sessions"][0]["title"]
        assert server.PREAMBLE_OPEN not in title

    def test_unparseable_output_yields_an_empty_list(self, client, fake_exec):
        fake_exec(stdout=b"No sessions found.\n")
        assert client.get("/api/sessions").json()["sessions"] == []


class TestSessionTranscript:
    def _export(self, messages):
        return json.dumps({"messages": messages}).encode()

    def test_normalizes_roles(self, client, fake_exec):
        fake_exec(stdout=self._export([
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]))
        messages = client.get("/api/session/20260715_193102_62eba9").json()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[1]["text"] == "answer"

    def test_joins_structured_content_blocks(self, client, fake_exec):
        fake_exec(stdout=self._export([
            {"role": "assistant",
             "content": [{"text": "part one "}, {"text": "part two"}]},
        ]))
        messages = client.get("/api/session/20260715_193102_62eba9").json()["messages"]
        assert messages[0]["text"] == "part one part two"

    def test_tool_calls_attach_to_the_assistant_turn(self, client, fake_exec):
        fake_exec(stdout=self._export([
            {"role": "assistant", "content": "working",
             "tool_calls": [{"function": {"name": "read_file",
                                          "arguments": {"path": "/x"}}}]},
            {"role": "tool", "content": "file contents here"},
        ]))
        messages = client.get("/api/session/20260715_193102_62eba9").json()["messages"]
        assert len(messages) == 1
        assert "read_file" in messages[0]["tools"][0]
        assert any(t.startswith("↳") for t in messages[0]["tools"])

    def test_the_preamble_is_hidden_from_the_user_turn(self, client, fake_exec):
        fake_exec(stdout=self._export([
            {"role": "user",
             "content": f"{server.PREAMBLE_OPEN}ctx{server.PREAMBLE_CLOSE}\nreal ask"},
        ]))
        messages = client.get("/api/session/20260715_193102_62eba9").json()["messages"]
        assert messages[0]["text"] == "real ask"

    def test_a_bad_session_id_is_rejected(self, client):
        assert client.get("/api/session/..%2F..%2Fetc").status_code in (400, 404)

    def test_empty_export_carries_the_stderr_note(self, client, fake_exec):
        fake_exec(stdout=b"", stderr=b"session not found")
        body = client.get("/api/session/20260715_193102_62eba9").json()
        assert body["messages"] == []
        assert "session not found" in body["note"]


class TestModels:
    CONFIG = """
model:
  default: google/gemma-4-26b
  provider: local
fallback_providers:
  - model: gemini-2.5-pro
    provider: google
    context_length: 1000000
  - model: broken-entry
"""

    def test_lists_the_primary_and_fallbacks(self, client, fake_run):
        fake_run(self.CONFIG)
        body = client.get("/api/models").json()
        assert body["primary"] == {"model": "google/gemma-4-26b", "provider": "local"}
        assert [o["model"] for o in body["options"]] == ["gemini-2.5-pro"]

    def test_context_length_rides_along(self, client, fake_run):
        fake_run(self.CONFIG)
        assert client.get("/api/models").json()["options"][0]["context_length"] == 1000000

    def test_an_entry_missing_a_provider_is_not_offered(self, client, fake_run):
        """Hermes' own get_fallback_chain() skips these, so offering one would
        put a menu item in the picker that can never be selected."""
        fake_run(self.CONFIG)
        assert all(o["model"] != "broken-entry"
                   for o in client.get("/api/models").json()["options"])

    def test_unparseable_config_is_not_a_500(self, client, fake_run):
        fake_run("::: not yaml :::")
        response = client.get("/api/models")
        assert response.status_code == 200
        assert response.json()["options"] == []

    def test_the_result_is_cached(self, client, fake_run):
        fake_run(self.CONFIG)
        client.get("/api/models")
        client.get("/api/models")
        assert len(fake_run.calls) == 1


class TestSkills:
    TABLE = (
        "│ pdf   │ docs │ bundled │ core  │ enabled │\n"
        "│ xlsx  │ docs │ user    │ local │ enabled │\n"
    )

    def test_groups_by_category_and_source(self, client, fake_run):
        fake_run(self.TABLE)
        body = client.get("/api/skills").json()
        assert body["total"] == 2
        assert body["categories"] == {"docs": 2}
        assert body["sources"] == {"bundled": 1, "user": 1}

    def test_reports_a_parse_failure_without_erroring(self, client, fake_run):
        fake_run("command not found")
        body = client.get("/api/skills")
        assert body.status_code == 200
        assert body.json()["error"]

    def test_refresh_bypasses_the_cache(self, client, fake_run):
        fake_run(self.TABLE)
        client.get("/api/skills")
        client.get("/api/skills?refresh=1")
        assert len(fake_run.calls) == 2


class TestChatStreaming:
    """The SSE path through the middleware stack.

    BaseHTTPMiddleware wrapping a StreamingResponse is the classic place for a
    stream to be buffered or truncated, and adding the security-headers
    middleware put a second wrapper around the chat stream. These assert the
    reply still arrives incrementally and still carries the headers.
    """

    @pytest.fixture
    def fake_stream(self, monkeypatch):
        async def _stream(history, message, session, *args, **kwargs):
            yield b"event: start\ndata: {}\n\n"
            yield b"event: chunk\ndata: {\"text\": \"hello\"}\n\n"
            yield b"event: done\ndata: {\"code\": 0}\n\n"
        monkeypatch.setattr(server, "_stream_chat", _stream)

    def test_reply_streams_as_sse(self, client, fake_stream):
        response = client.post("/api/chat",
                               json={"message": "hi", "session": "c_abc__1"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: chunk" in response.text
        assert "hello" in response.text

    def test_stream_is_not_buffered_by_a_proxy(self, client, fake_stream):
        headers = client.post("/api/chat",
                              json={"message": "hi", "session": "c_abc__1"}).headers
        assert headers["X-Accel-Buffering"] == "no"

    def test_security_headers_reach_the_stream(self, client, fake_stream):
        headers = client.post("/api/chat",
                              json={"message": "hi", "session": "c_abc__1"}).headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" in headers

    def test_the_streaming_cache_header_is_not_clobbered(self, client, fake_stream):
        """/api/chat sets its own Cache-Control; the middleware uses
        setdefault so it must not overwrite it."""
        headers = client.post("/api/chat",
                              json={"message": "hi", "session": "c_abc__1"}).headers
        assert "no-cache" in headers["Cache-Control"]

    def test_auth_still_applies_to_the_stream(self, client, token, fake_stream):
        response = client.post("/api/chat",
                               json={"message": "hi", "session": "c_abc__1"})
        assert response.status_code == 401


class TestStop:
    def test_reports_when_there_was_no_local_process(self, client, fake_exec):
        fake_exec()
        body = client.post("/api/stop", json={"session": "c_abc__1"}).json()
        assert body["ok"] is True
        assert body["had_local_process"] is False

    def test_rejects_an_empty_session(self, client):
        assert client.post("/api/stop", json={"session": "  "}).status_code == 400


class TestCompact:
    """/api/compact folds a conversation into notes the client swaps in.

    The failure that matters here is not a bad summary — it is a compaction
    that reports success while returning nothing usable, because the client
    then replaces real history with it.
    """

    HISTORY = [
        {"role": "user", "text": "set up the deploy"},
        {"role": "assistant", "text": "installed it at /opt/app"},
        {"role": "user", "text": "now add TLS"},
    ]
    SUMMARY = ("goal: deploy the app and put TLS in front of it\n"
               "done: installed at /opt/app\n"
               "open: TLS not configured yet")

    def test_returns_the_summary(self, client, fake_run):
        fake_run(self.SUMMARY)
        body = client.post("/api/compact", json={"history": self.HISTORY}).json()
        assert body["summary"] == self.SUMMARY
        assert body["turns"] == 3

    def test_the_prompt_carries_the_history_and_the_directive(self, client, fake_run,
                                                              monkeypatch):
        monkeypatch.setattr(server, "COMPACT_DIRECTIVE", "SUMMARIZE-DIRECTIVE")
        fake_run(self.SUMMARY)
        client.post("/api/compact", json={"history": self.HISTORY})
        prompt = fake_run.calls[0][fake_run.calls[0].index("-z") + 1]
        assert "User: set up the deploy" in prompt
        assert "Assistant: installed it at /opt/app" in prompt
        assert "SUMMARIZE-DIRECTIVE" in prompt

    def test_no_reply_framing_leaks_into_the_compaction_prompt(self, client, fake_run):
        """The chat prompt ends by telling the model to answer the user. A
        compaction that inherited that framing answers the last message
        instead of summarizing the conversation."""
        fake_run(self.SUMMARY)
        client.post("/api/compact", json={"history": self.HISTORY})
        prompt = fake_run.calls[0][fake_run.calls[0].index("-z") + 1]
        assert "Now reply to the latest user message" not in prompt

    def test_the_focus_topic_reaches_the_prompt(self, client, fake_run):
        fake_run(self.SUMMARY)
        client.post("/api/compact",
                    json={"history": self.HISTORY, "focus": "the TLS work"})
        prompt = fake_run.calls[0][fake_run.calls[0].index("-z") + 1]
        assert "the TLS work" in prompt

    def test_the_model_override_is_passed_through(self, client, fake_run):
        fake_run(self.SUMMARY)
        client.post("/api/compact", json={"history": self.HISTORY,
                                          "model": "gemini-3-flash",
                                          "provider": "google"})
        args = fake_run.calls[0]
        assert "gemini-3-flash" in args and "google" in args

    def test_a_tool_trace_is_stripped_from_the_summary(self, client, fake_run):
        """--yolo stays on so a stray tool call can't hang the request on a
        confirmation. The trace it prints must not become part of the notes."""
        fake_run("● read_file {\"path\": \"/opt/app\"}\n↳ ok\n" + self.SUMMARY)
        body = client.post("/api/compact", json={"history": self.HISTORY}).json()
        assert body["summary"] == self.SUMMARY

    def test_output_with_nothing_but_a_trace_is_an_error(self, client, fake_run):
        """A summary of "" would wipe the conversation it replaced."""
        fake_run("● read_file {}\n↳ ok")
        response = client.post("/api/compact", json={"history": self.HISTORY})
        assert response.status_code == 502
        assert "summary" not in response.json()

    def test_too_little_history_is_refused(self, client):
        response = client.post("/api/compact",
                               json={"history": [{"role": "user", "text": "hi"}]})
        assert response.status_code == 400

    def test_blank_turns_do_not_count_as_history(self, client):
        response = client.post("/api/compact", json={"history": [
            {"role": "user", "text": "hi"},
            {"role": "assistant", "text": "   "},
            {"role": "assistant", "text": ""}]})
        assert response.status_code == 400

    def test_a_bad_session_key_is_refused(self, client):
        """The key is interpolated into the pkill pattern used on timeout."""
        response = client.post("/api/compact",
                               json={"history": self.HISTORY, "session": "../../x"})
        assert response.status_code == 400

    def test_a_compaction_is_not_a_turn(self, client, fake_run, turns_dir):
        """It transforms the client's own state; a record of it would surface
        in /api/turns as a turn nobody sent."""
        fake_run(self.SUMMARY)
        client.post("/api/compact", json={"history": self.HISTORY})
        assert server.TURNS == {}
        assert list(turns_dir.iterdir()) == []

    def test_a_cli_error_is_not_stored_as_a_summary(self, client, fake_run):
        """Observed against a live agent: the CLI printed "API call failed
        after 3 retries: Connection error." on stdout and exited 0. Returned as
        a summary, that sentence REPLACES the conversation it was meant to
        preserve — so the text is checked, not the exit code."""
        fake_run("API call failed after 3 retries: Connection error.", code=0)
        response = client.post("/api/compact", json={"history": self.HISTORY})
        assert response.status_code == 502
        assert "summary" not in response.json()
        assert "Connection error" in response.json()["error"]

    def test_a_traceback_is_not_stored_as_a_summary(self, client, fake_run):
        fake_run("Traceback (most recent call last):\n" + "  File x, line 1\n" * 20)
        assert client.post("/api/compact",
                           json={"history": self.HISTORY}).status_code == 502

    def test_a_summary_too_short_to_carry_anything_is_refused(self, client, fake_run):
        """Catches the failure lines no vocabulary list anticipated."""
        fake_run("ok")
        response = client.post("/api/compact", json={"history": self.HISTORY})
        assert response.status_code == 502
        assert "2 characters" in response.json()["error"]

    def test_the_floor_is_configurable(self, client, fake_run, monkeypatch):
        monkeypatch.setattr(server, "COMPACT_MIN_CHARS", 1)
        fake_run("ok")
        assert client.post("/api/compact",
                           json={"history": self.HISTORY}).json()["summary"] == "ok"

    def test_a_summary_may_still_discuss_an_error(self, client, fake_run):
        """The signature check must anchor at the start of the output, or a
        conversation ABOUT a failure can never be compacted."""
        fake_run("goal: fix the deploy\n"
                 "fact: the agent logged 'API call failed after 3 retries'\n"
                 "open: still need to check the LLM endpoint")
        assert client.post("/api/compact",
                           json={"history": self.HISTORY}).status_code == 200

    def test_a_timeout_kills_the_container_process(self, client, fake_exec,
                                                   monkeypatch):
        """asyncio.wait_for abandons the coroutine but leaves the hermes run
        inside the container burning the model."""
        import asyncio as _asyncio

        async def _hang(*args, **kwargs):
            await _asyncio.sleep(5)
            return 0, "never"

        monkeypatch.setattr(server, "_run", _hang)
        monkeypatch.setattr(server, "COMPACT_TIMEOUT", 0.01)
        fake_exec()
        response = client.post("/api/compact", json={"history": self.HISTORY})
        assert response.status_code == 504
        assert any("pkill" in a for call in fake_exec.calls for a in call)
