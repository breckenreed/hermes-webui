"""Turn records: the on-disk copy of a reply, and the thing that makes a
dropped phone or a restarted webui recoverable rather than a lost answer.
"""
import json
import os
import time

import server


def _write(turns_dir, key, rec=None, age_seconds=0.0):
    path = turns_dir / f"{key}.json"
    path.write_text(json.dumps(rec or {"status": "done", "text": key}),
                    encoding="utf-8")
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


class TestKeyValidation:
    """A turn key becomes a filename and a `pkill -f` regex, so it is the one
    piece of client input that must never be taken at face value."""

    def test_ordinary_keys_are_accepted(self):
        for key in ("webui_default", "20260715_193102_62eba9", "a.b-c_1"):
            assert server._valid_turn_key(key)

    def test_path_traversal_is_rejected(self):
        for key in ("../../etc/passwd", "a/b", "..", "/abs"):
            assert not server._valid_turn_key(key)

    def test_regex_metacharacters_are_rejected(self):
        # These would change what /api/stop's `pkill -f` pattern matches.
        for key in (".*", "a|b", "x$", "(y)", "a+"):
            assert not server._valid_turn_key(key)

    def test_empty_and_overlong_are_rejected(self):
        assert not server._valid_turn_key("")
        assert not server._valid_turn_key("a" * 121)

    def test_a_rejected_key_never_reaches_the_filesystem(self, turns_dir):
        server._persist_turn("../escape", {"status": "done"})
        assert not (turns_dir.parent / "escape.json").exists()
        assert list(turns_dir.iterdir()) == []


class TestPersistence:
    def test_roundtrip(self, turns_dir):
        rec = {"status": "done", "text": "hello", "events": [{"kind": "chunk"}]}
        server._persist_turn("s1", rec)
        assert server._load_turn("s1") == rec

    def test_missing_record_reads_as_none(self, turns_dir):
        assert server._load_turn("never-written") is None

    def test_write_leaves_no_temp_file_behind(self, turns_dir):
        server._persist_turn("s1", {"status": "done"})
        assert [p.name for p in turns_dir.iterdir()] == ["s1.json"]

    def test_a_failed_write_leaves_the_previous_record_intact(
            self, turns_dir, monkeypatch):
        """This is what the write-then-rename buys.

        Writing in place means a failure part-way through has already
        destroyed the record that was there. Staging to a sibling and renaming
        means a failed write changes nothing a reader can see.
        """
        server._persist_turn("s1", {"status": "done", "text": "original"})

        def _fail(src, dst):
            raise OSError("no space left on device")
        monkeypatch.setattr(server.os, "replace", _fail)

        server._persist_turn("s1", {"status": "done", "text": "replacement"})

        assert server._load_turn("s1")["text"] == "original"
        assert list(turns_dir.glob("*.tmp")) == []

    def test_corrupt_record_is_discarded_not_raised(self, turns_dir):
        """A truncated file (killed mid-write, before atomic writes) must not
        keep failing the recovery path on every retry."""
        path = turns_dir / "s1.json"
        path.write_text('{"status": "do', encoding="utf-8")
        assert server._load_turn("s1") is None
        assert not path.exists()

    def test_a_later_write_recovers_after_corruption(self, turns_dir):
        (turns_dir / "s1.json").write_text("{{{", encoding="utf-8")
        server._load_turn("s1")
        server._persist_turn("s1", {"status": "done", "text": "recovered"})
        assert server._load_turn("s1")["text"] == "recovered"

    def test_drop_removes_both_copies(self, turns_dir):
        server.TURNS["s1"] = {"status": "done"}
        server._persist_turn("s1", {"status": "done"})
        server._drop_turn("s1")
        assert "s1" not in server.TURNS
        assert not (turns_dir / "s1.json").exists()


class TestRetention:
    def test_prune_keeps_the_newest_by_count(self, turns_dir, monkeypatch):
        monkeypatch.setattr(server, "TURNS_MAX_FILES", 3)
        monkeypatch.setattr(server, "TURNS_MAX_AGE_DAYS", 0)
        for i in range(10):
            _write(turns_dir, f"s{i}", age_seconds=10 - i)   # s9 newest

        removed = server._prune_turn_files()

        survivors = sorted(p.stem for p in turns_dir.glob("*.json"))
        assert removed == 7
        assert survivors == ["s7", "s8", "s9"]

    def test_prune_drops_records_past_the_age_limit(self, turns_dir, monkeypatch):
        monkeypatch.setattr(server, "TURNS_MAX_FILES", 0)
        monkeypatch.setattr(server, "TURNS_MAX_AGE_DAYS", 7)
        _write(turns_dir, "fresh", age_seconds=3600)
        _write(turns_dir, "ancient", age_seconds=30 * 86400)

        server._prune_turn_files()

        assert (turns_dir / "fresh.json").exists()
        assert not (turns_dir / "ancient.json").exists()

    def test_age_limit_can_be_disabled(self, turns_dir, monkeypatch):
        monkeypatch.setattr(server, "TURNS_MAX_FILES", 0)
        monkeypatch.setattr(server, "TURNS_MAX_AGE_DAYS", 0)
        _write(turns_dir, "ancient", age_seconds=365 * 86400)
        assert server._prune_turn_files() == 0
        assert (turns_dir / "ancient.json").exists()

    def test_prune_sweeps_abandoned_temp_files(self, turns_dir, monkeypatch):
        monkeypatch.setattr(server, "TURNS_MAX_FILES", 0)
        monkeypatch.setattr(server, "TURNS_MAX_AGE_DAYS", 0)
        stale = turns_dir / "s1.json.tmp"
        stale.write_text("{}", encoding="utf-8")
        old = time.time() - 3600
        os.utime(stale, (old, old))

        server._prune_turn_files()
        assert not stale.exists()

    def test_prune_leaves_an_in_progress_write_alone(self, turns_dir, monkeypatch):
        monkeypatch.setattr(server, "TURNS_MAX_FILES", 0)
        monkeypatch.setattr(server, "TURNS_MAX_AGE_DAYS", 0)
        fresh = turns_dir / "s1.json.tmp"
        fresh.write_text("{}", encoding="utf-8")

        server._prune_turn_files()
        assert fresh.exists()

    def test_prune_on_an_empty_directory_is_harmless(self, turns_dir):
        assert server._prune_turn_files() == 0


class TestRestore:
    def test_records_come_back_after_a_restart(self, turns_dir):
        _write(turns_dir, "s1", {"status": "done", "text": "kept"})
        assert server._restore_turns() == 1
        assert server.TURNS["s1"]["text"] == "kept"

    def test_restore_is_capped_at_the_memory_limit(self, turns_dir, monkeypatch):
        monkeypatch.setattr(server, "_TURNS_MAX", 5)
        for i in range(20):
            _write(turns_dir, f"s{i}", age_seconds=20 - i)
        assert server._restore_turns() == 5

    def test_restore_prefers_the_newest(self, turns_dir, monkeypatch):
        monkeypatch.setattr(server, "_TURNS_MAX", 1)   # only one can fit
        _write(turns_dir, "old", age_seconds=1000)
        _write(turns_dir, "new", age_seconds=1)
        server._restore_turns()
        assert "new" in server.TURNS
        assert "old" not in server.TURNS

    def test_restore_skips_corrupt_records(self, turns_dir):
        _write(turns_dir, "good")
        (turns_dir / "bad.json").write_text("not json", encoding="utf-8")
        assert server._restore_turns() == 1

    def test_restore_does_not_clobber_a_live_record(self, turns_dir):
        server.TURNS["s1"] = {"status": "running", "text": "live"}
        _write(turns_dir, "s1", {"status": "done", "text": "stale"})
        server._restore_turns()
        assert server.TURNS["s1"]["text"] == "live"


class TestDirectoryResolution:
    def test_explicit_setting_wins(self, tmp_path, monkeypatch):
        target = tmp_path / "custom"
        monkeypatch.setenv("TURNS_DIR", str(target))
        assert server._resolve_turns_dir() == target
        assert target.is_dir()

    def test_falls_back_when_the_preferred_path_is_unwritable(
            self, tmp_path, monkeypatch):
        """A missing or read-only mount must degrade to a working directory
        rather than taking the server down at import."""
        monkeypatch.setenv("TURNS_DIR", "/proc/nonexistent/turns")
        resolved = server._resolve_turns_dir()
        assert resolved.is_dir()
        assert resolved != tmp_path / "custom"


class TestTurnsEndpoint:
    def test_lists_persisted_records_newest_first(self, client, turns_dir):
        _write(turns_dir, "older", {"status": "done", "text": "first reply"},
               age_seconds=500)
        _write(turns_dir, "newer", {"status": "done", "text": "second reply"},
               age_seconds=1)

        body = client.get("/api/turns").json()

        assert [t["session"] for t in body["turns"]] == ["newer", "older"]
        assert body["turns"][0]["preview"] == "second reply"
        assert body["total"] == 2

    def test_preview_is_truncated(self, client, turns_dir):
        _write(turns_dir, "s1", {"status": "done", "text": "x" * 1000})
        turn = client.get("/api/turns").json()["turns"][0]
        assert len(turn["preview"]) == 180
        assert turn["chars"] == 1000

    def test_limit_is_clamped(self, client, turns_dir):
        for i in range(5):
            _write(turns_dir, f"s{i}")
        assert len(client.get("/api/turns?limit=2").json()["turns"]) == 2
        assert client.get("/api/turns?limit=99999").status_code == 200

    def test_empty_directory_is_not_an_error(self, client, turns_dir):
        assert client.get("/api/turns").json() == {
            "turns": [], "dir": str(turns_dir), "total": 0}


class TestEndpointKeyValidation:
    def test_turn_lookup_rejects_a_bad_key(self, client, turns_dir):
        response = client.get("/api/turn/a$b")
        assert response.status_code == 400
        assert response.json()["error"] == "bad turn key"

    def test_an_encoded_slash_never_reaches_the_handler(self, client, turns_dir):
        """Starlette decodes %2F before routing, so a traversal attempt fails
        to match the route at all. Pinned so a future route change that made
        the path greedy would surface here."""
        assert client.get("/api/turn/..%2F..%2Fetc").status_code == 404

    def test_ack_rejects_a_bad_key(self, client, turns_dir):
        assert client.post("/api/turn/a$b/ack").status_code == 400

    def test_chat_rejects_a_bad_session_key(self, client, turns_dir):
        response = client.post("/api/chat",
                               json={"message": "hi", "session": "../../pwn"})
        assert response.status_code == 400
        assert response.json()["error"] == "bad session key"

    def test_stop_rejects_a_bad_session_key(self, client, turns_dir):
        response = client.post("/api/stop", json={"session": ".*"})
        assert response.status_code == 400

    def test_chat_still_rejects_an_empty_message(self, client, turns_dir):
        response = client.post("/api/chat", json={"message": "  ", "session": "s1"})
        assert response.status_code == 400
        assert response.json()["error"] == "empty message"
