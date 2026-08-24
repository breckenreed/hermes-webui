"""How credentials move — or fail to move — between the webui and the agent.

Command lines are not private: `docker exec -e K=V` puts V where any local
user's `ps` can read it. These tests pin down that the key rides there only
when the container genuinely has no key of its own.
"""
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

    @pytest.fixture
    def fake_run(self, monkeypatch):
        def install(text, code=0):
            async def _run(*args, **kwargs):
                install.calls.append(args)
                return code, text
            monkeypatch.setattr(server, "_run", _run)
        install.calls = []
        return install

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
