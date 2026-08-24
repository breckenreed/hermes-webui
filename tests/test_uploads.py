"""Attachments: getting a file into a container this project does not own.

The agent runs elsewhere, so the file is written to the webui's state volume
and `docker cp`-ed across. A filename crosses two path boundaries on that trip,
which is why most of these tests are about names rather than bytes.
"""
import base64

import pytest

import server


def payload(name="note.txt", data=b"hello", convo="c_abc"):
    return {"name": name, "data": base64.b64encode(data).decode(), "convo": convo}


class TestFilenames:
    """Names are rebuilt from a known-safe alphabet rather than sanitised, so
    there is no blacklist to have missed something."""

    @pytest.mark.parametrize("given,forbidden", [
        ("../../etc/passwd", "/"),
        ("x;rm -rf /.sh", ";"),
        ("my photo.png", " "),
    ])
    def test_dangerous_characters_do_not_survive(self, given, forbidden):
        assert forbidden not in server._safe_filename(given)

    def test_a_leading_dash_is_dropped(self):
        """It would otherwise be read as a flag by the CLI on the other side."""
        assert not server._safe_filename("-rf").startswith("-")

    def test_a_name_that_is_entirely_unsafe_still_yields_something(self):
        assert server._safe_filename("..") == "file"
        assert server._safe_filename("") == "file"

    def test_ordinary_names_are_left_recognisable(self):
        assert server._safe_filename("docker-compose.yml") == "docker-compose.yml"


class TestUpload:
    def test_a_file_lands_in_the_agents_attachment_directory(self, client, fake_run):
        fake_run("")
        body = client.post("/api/upload", json=payload()).json()
        assert body["name"] == "note.txt"
        assert body["bytes"] == 5
        assert body["path"].startswith(server.AGENT_ATTACH_DIR)
        assert body["path"].endswith("/note.txt")

    def test_it_never_lands_in_the_workspace(self, client, fake_run):
        """An attachment dropped where the agent happens to be working turns
        "look at this" into an edit to the project."""
        fake_run("")
        path = client.post("/api/upload", json=payload()).json()["path"]
        assert "/workspace" not in path
        assert path.startswith(server.AGENT_ATTACH_DIR + "/")

    def test_a_traversal_attempt_stays_inside_that_directory(self, client, fake_run):
        fake_run("")
        body = client.post("/api/upload", json=payload(
            name="../../../../etc/cron.d/pwn", convo="../../root")).json()
        assert body["path"].startswith(server.AGENT_ATTACH_DIR + "/")
        assert "/etc/cron.d/" not in body["path"]
        assert ".." not in body["path"].replace("_..", "")

    def test_oversize_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(server, "MAX_UPLOAD_MB", 0.001)
        response = client.post("/api/upload", json=payload(data=b"x" * 5000))
        assert response.status_code == 413
        assert "limit" in response.json()["error"]

    def test_a_declared_length_over_the_cap_is_refused_before_decoding(
            self, client, monkeypatch):
        """Refusing a huge upload should not require holding it first."""
        monkeypatch.setattr(server, "MAX_UPLOAD_MB", 0.0001)
        response = client.post("/api/upload", json=payload(data=b"x" * 4000))
        assert response.status_code == 413

    def test_something_that_is_not_base64_is_a_clear_error(self, client):
        response = client.post("/api/upload",
                               json={"name": "x", "data": "!!!!", "convo": "c_abc"})
        assert response.status_code == 400
        assert "base64" in response.json()["error"]

    def test_an_empty_attachment_is_refused(self, client):
        response = client.post("/api/upload",
                               json={"name": "x", "data": "", "convo": "c_abc"})
        assert response.status_code == 400

    def test_a_failing_copy_is_reported_not_swallowed(self, client, fake_run):
        """Otherwise the browser shows a path to a file that is not there, and
        the failure only surfaces as the agent 'not finding' it mid-turn."""
        fake_run("no such container", code=1)
        response = client.post("/api/upload", json=payload())
        assert response.status_code == 502
        assert "path" not in response.json()

    def test_each_upload_gets_its_own_directory(self, client, fake_run):
        """Two files with the same name must not overwrite each other."""
        fake_run("")
        first = client.post("/api/upload", json=payload()).json()["path"]
        second = client.post("/api/upload", json=payload()).json()["path"]
        assert first != second

    def test_uploads_require_the_token_when_one_is_set(self, client, token):
        """Unlike the PWA assets, this one writes into the agent container."""
        assert client.post("/api/upload", json=payload()).status_code == 401
