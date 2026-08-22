"""Shared fixtures.

server.py is a module-level script: it resolves TURNS_DIR and reads
WEBUI_TOKEN at import time. So the environment is arranged BEFORE the import
below, and per-test overrides go through monkeypatch on the module attribute
(every consumer looks the value up as a global at call time, so patching the
attribute is enough — no reimport needed).
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the records directory somewhere disposable before importing, so a test
# run can never read or delete a real turn record.
os.environ["TURNS_DIR"] = tempfile.mkdtemp(prefix="hermes-webui-tests-")
os.environ.pop("WEBUI_TOKEN", None)
os.environ.pop("WEBUI_TLS", None)

import server  # noqa: E402


class RealSubprocessAttempted(BaseException):
    """Raised when a test reaches for docker.

    Deliberately NOT an Exception subclass: server.py wraps every docker call
    in a broad `except Exception`, which would swallow this and turn a
    hermetic-suite violation into a test that silently passes against a
    half-mocked world.
    """


@pytest.fixture(autouse=True)
def no_real_subprocesses(monkeypatch):
    """Keep the suite hermetic.

    A test that actually shells out is slow, needs a running agent container,
    and can act on the real system. Tests that need a subprocess install their
    own stand-in (see the fake_exec fixture in test_endpoints.py), which takes
    precedence over this.
    """
    async def _refuse(*args, **kwargs):
        raise RealSubprocessAttempted(
            "test tried to spawn a real subprocess: "
            + " ".join(str(a) for a in args[:4]))

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", _refuse)


@pytest.fixture
def turns_dir(tmp_path, monkeypatch):
    """An isolated on-disk records directory, with an empty memory table."""
    path = tmp_path / "turns"
    path.mkdir()
    monkeypatch.setattr(server, "TURNS_DIR", path)
    monkeypatch.setattr(server, "TURNS", {})
    return path


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    with TestClient(server.app) as test_client:
        yield test_client


@pytest.fixture
def token(monkeypatch):
    """Enable token auth with a known value and a clean failure table."""
    value = "test-token-0123456789abcdef"
    monkeypatch.setattr(server, "WEBUI_TOKEN", value)
    monkeypatch.setattr(server, "_AUTH_FAILURES", {})
    return value


@pytest.fixture
def auth(token):
    return {"Authorization": f"Bearer {token}"}
