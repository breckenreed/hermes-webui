"""Scheduled jobs: a control panel over `hermes cron`.

The webui schedules nothing itself. These tests are mostly about the parser,
because the CLI has no --json mode and the difference between "no jobs" and
"we could not read the answer" is the difference between a panel that is
telling the truth and one that is not.
"""
import pytest

import server

LISTING = """
┌──────────────────────────────┐
│        Scheduled Jobs        │
└──────────────────────────────┘

  3e5bbe3b111a [active]
    Name:      morning summary
    Schedule:  0 9 * * *
    Repeat:    inf
    Next run:  2026-08-25T09:00:00+00:00
    Deliver:   local

  f6599906a597 [paused]
    Name:      weekly digest
    Schedule:  0 8 * * 1
    Next run:  2026-08-31T08:00:00+00:00

  ⚠  Gateway is not running — jobs won't fire automatically.
"""


class TestParsing:
    def test_reads_every_job_and_its_fields(self):
        jobs, readable = server._parse_cron_list(LISTING)
        assert readable is True
        assert [j["id"] for j in jobs] == ["3e5bbe3b111a", "f6599906a597"]
        assert jobs[0]["name"] == "morning summary"
        assert jobs[0]["schedule"] == "0 9 * * *"
        assert jobs[0]["deliver"] == "local"

    def test_a_paused_job_keeps_its_state(self):
        """The panel asks for --all precisely so pausing does not look like
        deleting; the state has to survive to the UI."""
        jobs, _ = server._parse_cron_list(LISTING)
        assert jobs[1]["state"] == "paused"

    def test_no_jobs_is_readable_and_empty(self):
        assert server._parse_cron_list("No scheduled jobs.\nCreate one with…") == ([], True)

    def test_output_we_cannot_parse_is_not_reported_as_empty(self):
        """An empty list and an unreadable answer look identical on screen, and
        only one of them means the panel is right."""
        jobs, readable = server._parse_cron_list("!!! something unexpected !!!")
        assert jobs == [] and readable is False

    def test_ansi_colouring_does_not_break_it(self):
        jobs, _ = server._parse_cron_list("\x1b[32m" + LISTING + "\x1b[0m")
        assert len(jobs) == 2

    def test_the_scheduler_warning_is_detected(self):
        assert server._scheduler_running(LISTING) is False
        assert server._scheduler_running("  Cron scheduler is running") is True


class TestEndpoints:
    def test_the_list_reports_whether_anything_will_run_them(self, client, fake_run):
        fake_run(LISTING)
        body = client.get("/api/cron").json()
        assert len(body["jobs"]) == 2
        assert body["scheduler_running"] is False
        assert "not running" in body["note"]

    def test_it_asks_for_disabled_jobs_too(self, client, fake_run):
        """Without --all a paused job vanishes from the list, and pausing one
        would look exactly like deleting it."""
        fake_run(LISTING)
        client.get("/api/cron")
        assert "--all" in fake_run.calls[0]

    def test_creating_requires_a_schedule(self, client):
        assert client.post("/api/cron", json={"schedule": "   "}).status_code == 400

    def test_creating_passes_the_schedule_and_name(self, client, fake_run):
        fake_run("Created job: abc123abc123")
        client.post("/api/cron", json={"schedule": "0 9 * * *", "prompt": "summarise",
                                       "name": "morning"})
        args = fake_run.calls[0]
        assert "0 9 * * *" in args and "summarise" in args
        assert "--name" in args and "morning" in args

    def test_a_cli_failure_is_surfaced_rather_than_reported_as_success(
            self, client, fake_run):
        fake_run("error: bad cron expression", code=1)
        response = client.post("/api/cron", json={"schedule": "not a schedule"})
        assert response.status_code == 400
        assert "bad cron expression" in response.json()["error"]

    @pytest.mark.parametrize("action", ["pause", "resume", "run"])
    def test_actions_reach_the_cli(self, client, fake_run, action):
        fake_run("ok")
        assert client.post(f"/api/cron/3e5bbe3b111a/{action}").status_code == 200
        assert action in fake_run.calls[0]

    def test_an_unknown_action_is_refused(self, client):
        assert client.post("/api/cron/3e5bbe3b111a/destroy").status_code == 400

    @pytest.mark.parametrize("bad", ["../../etc", "3e5b;rm -rf /", "zzz", "-rf"])
    def test_a_bad_job_id_never_reaches_the_cli(self, client, fake_run, bad):
        """The id becomes a CLI argument, so what matters is that nothing runs.

        Some of these are refused by routing before the handler ever sees them
        (anything containing a slash cannot match the path), and the rest by
        the id check. Both are refusals; asserting one status code would be
        asserting which layer said no, which is not the property worth pinning.
        """
        fake_run("should never run")
        assert client.post(f"/api/cron/{bad}/pause").status_code in (400, 404)
        assert client.delete(f"/api/cron/{bad}").status_code in (400, 404)
        assert fake_run.calls == []

    def test_removal_reaches_the_cli(self, client, fake_run):
        fake_run("Removed job: x")
        assert client.delete("/api/cron/3e5bbe3b111a").status_code == 200
        assert "remove" in fake_run.calls[0]

    def test_run_history_is_redacted_like_any_other_agent_output(self, client, fake_run):
        fake_run("attempt 1: GITHUB_TOKEN=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        assert "ghp_AAAA" not in client.get("/api/cron/3e5bbe3b111a/runs").json()["text"]
