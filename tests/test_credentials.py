"""How credentials move — or fail to move — between the webui and the agent.

Command lines are not private: `docker exec -e K=V` puts V where any local
user's `ps` can read it. These tests pin down that the key rides there only
when the container genuinely has no key of its own.
"""
import json

import pytest

import server


@pytest.fixture(autouse=True)
def key_set(monkeypatch):
    """A webui that holds a key at all — otherwise there is nothing to pass."""
    monkeypatch.setattr(server, "LLM_CLIENT_UID", "sk-test-key")
    monkeypatch.setattr(server, "FORCE_PASS_LLM_KEY", False)
    monkeypatch.setattr(server, "AGENT_HAS_LLM_KEY", False)


def _joined(args):
    return " ".join(args)


class TestExecPrefix:
    def test_the_key_is_passed_when_the_container_has_none(self, monkeypatch):
        monkeypatch.setattr(server, "AGENT_HAS_LLM_KEY", False)
        assert "sk-test-key" in _joined(server._exec_prefix())

    def test_the_key_is_withheld_once_the_container_is_known_to_have_it(
            self, monkeypatch):
        monkeypatch.setattr(server, "AGENT_HAS_LLM_KEY", True)
        assert "sk-test-key" not in _joined(server._exec_prefix())

    def test_the_override_forces_it_back_onto_the_command_line(self, monkeypatch):
        """For a container whose baked-in key is stale."""
        monkeypatch.setattr(server, "AGENT_HAS_LLM_KEY", True)
        monkeypatch.setattr(server, "FORCE_PASS_LLM_KEY", True)
        assert "sk-test-key" in _joined(server._exec_prefix())

    def test_a_webui_with_no_key_passes_nothing(self, monkeypatch):
        monkeypatch.setattr(server, "LLM_CLIENT_UID", "")
        assert "LLM_CLIENT_UID" not in _joined(server._exec_prefix())

    def test_non_secret_extra_env_still_rides_along(self, monkeypatch):
        """The COLUMNS widening /api/skills relies on must keep working."""
        monkeypatch.setattr(server, "AGENT_HAS_LLM_KEY", True)
        assert "COLUMNS=200" in _joined(server._exec_prefix({"COLUMNS": "200"}))

    def test_the_container_name_is_still_the_last_argument(self, monkeypatch):
        """Dropping the flag must not disturb where the command starts."""
        monkeypatch.setattr(server, "AGENT_HAS_LLM_KEY", True)
        assert server._exec_prefix()[-1] == server.HERMES_CONTAINER


class TestProbe:
    """The probe must answer the question without becoming the leak."""

    @pytest.mark.anyio
    async def test_reports_presence(self, fake_run):
        fake_run("present")
        assert await server._probe_agent_llm_key() is True

    @pytest.mark.anyio
    async def test_reports_absence(self, fake_run):
        fake_run("", code=1)
        assert await server._probe_agent_llm_key() is False

    @pytest.mark.anyio
    async def test_an_unreachable_container_is_absence_not_a_crash(self, monkeypatch):
        async def _boom(*args, **kwargs):
            raise OSError("docker is not running")
        monkeypatch.setattr(server, "_run", _boom)
        assert await server._probe_agent_llm_key() is False

    @pytest.mark.anyio
    async def test_the_probe_does_not_inject_the_key_it_is_testing_for(self, fake_run):
        """Injecting it would make the probe always answer yes, and would put
        the key on a command line to ask whether it is on command lines."""
        fake_run("present")
        await server._probe_agent_llm_key()
        assert "sk-test-key" not in _joined(fake_run.calls[0])

    @pytest.mark.anyio
    async def test_the_probe_never_asks_for_the_value(self, fake_run):
        """`echo $LLM_CLIENT_UID` would print the key into this process's
        output — the exact leak this whole change removes."""
        fake_run("present")
        await server._probe_agent_llm_key()
        shell_cmd = _joined(fake_run.calls[0])
        assert "test -n" in shell_cmd
        assert "echo $LLM_CLIENT_UID" not in shell_cmd
        assert "echo ${LLM_CLIENT_UID}" not in shell_cmd


# A stand-in for what an `environ` dump actually looked like in the transcript.
# Values here are invented; the shapes are the ones that showed up.
ENVIRON_DUMP = (
    "GITHUB_TOKEN=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\x00"
    "TAVILY_API_KEY=tvly-dev-BBBBBBBBBBBBBBBB\x00"
    "CLICKUP_API_KEY=pk_12345_CCCCCCCCCCCCCCCCCCCC\x00"
    "HOSTNAME=hermes-agent\x00PATH=/usr/local/bin:/usr/bin"
)


class TestRedaction:
    def test_an_environ_dump_loses_every_value(self):
        out = server._redact(ENVIRON_DUMP)
        for leaked in ("ghp_AAAA", "tvly-dev-BBBB", "pk_12345_CCCC"):
            assert leaked not in out

    def test_the_names_survive(self):
        """A transcript that quietly loses text is its own debugging problem —
        the reader has to be able to tell "nothing was there" from "it is
        hidden"."""
        out = server._redact(ENVIRON_DUMP)
        assert "GITHUB_TOKEN=" in out
        assert "TAVILY_API_KEY=" in out
        assert "<redacted>" in out

    def test_harmless_variables_are_left_alone(self):
        out = server._redact(ENVIRON_DUMP)
        assert "HOSTNAME=hermes-agent" in out
        assert "PATH=/usr/local/bin:/usr/bin" in out

    def test_a_bare_github_token_in_prose(self):
        """Not every leak arrives as NAME=value; a tool can just echo one."""
        out = server._redact("try again with ghp_ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ ok?")
        assert "ghp_ZZZZ" not in out
        assert "<redacted:token>" in out

    def test_a_fine_grained_pat(self):
        out = server._redact("github_pat_11ABCDEFG0aaaaaaaaaaaaaaaaaaaaaaaaaaa")
        assert "github_pat_11ABCDEFG" not in out

    def test_an_openai_style_key(self):
        out = server._redact("OPENAI key sk-proj-abcdefghijklmnopqrstuvwxyz012345")
        assert "sk-proj-abcdefgh" not in out

    def test_a_private_key_header(self):
        out = server._redact("-----BEGIN OPENSSH PRIVATE KEY-----\nbody\n")
        assert "BEGIN OPENSSH PRIVATE KEY" not in out
        assert "<redacted:private key>" in out

    def test_a_quoted_json_value(self):
        """Tool results arrive as JSON far more often than as shell output."""
        out = server._redact('{"CLICKUP_API_KEY": "pk_99999_DDDDDDDDDDDDDDDDDDDD"}')
        assert "pk_99999_DDDD" not in out
        assert '"<redacted>"' in out

    def test_ordinary_prose_is_untouched(self):
        text = "The turn records live in /app/state/turns and are pruned after 7 days."
        assert server._redact(text) == text

    def test_a_short_value_is_not_a_secret(self):
        """Keeps the rule from eating things like MONKEY=banana."""
        assert server._redact("MONKEY=banana") == "MONKEY=banana"

    def test_empty_input_is_safe(self):
        assert server._redact("") == ""
        assert server._redact(None) is None


class TestRedactionReachesTheTranscript:
    """Unit-level redaction is worth little if a path around it exists. These
    pin the two routes agent output actually travels."""

    @pytest.fixture
    def export_payload(self):
        import json
        return json.dumps({
            "model": "local",
            "messages": [
                {"role": "user", "content": "which port?"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "terminal",
                                  "arguments": {"command": "cat /proc/1/environ"}}}]},
                {"role": "tool", "content": ENVIRON_DUMP},
                {"role": "assistant", "content": "It listens on 8000."},
            ],
        }).encode()

    @pytest.mark.anyio
    async def test_tool_results_are_redacted_before_they_become_events(
            self, fake_exec, export_payload):
        """This is the path the observed leak actually took."""
        fake_exec(stdout=export_payload)
        events, _, _ = await server._export_turn("20260715_193102_62eba9")
        results = [e for e in events if e["kind"] == "result"]
        assert results, "expected a tool result event"
        assert "ghp_AAAA" not in results[0]["text"]
        assert "<redacted>" in results[0]["text"]

    def test_a_stored_session_is_redacted_when_reloaded(
            self, client, fake_exec, export_payload):
        """Records written before this change still hold the raw dump."""
        fake_exec(stdout=export_payload)
        body = client.get("/api/session/20260715_193102_62eba9").json()
        assert "ghp_AAAA" not in json.dumps(body)
        assert "tvly-dev-BBBB" not in json.dumps(body)
