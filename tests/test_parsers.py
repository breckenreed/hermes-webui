"""The parsing layer.

Every one of these functions exists because a Hermes CLI command has no
--json mode and its human-readable output has to be scraped. That makes them
the most fragile part of the project — an upstream formatting change breaks
them silently — and the part worth pinning down.
"""
import server


class TestComposePrompt:
    def test_carries_the_system_preamble(self, monkeypatch):
        monkeypatch.setattr(server, "SYSTEM_PREAMBLE", "PREAMBLE")
        prompt = server._compose_prompt([], "hello")
        assert prompt.startswith("PREAMBLE")
        assert prompt.endswith("User: hello")

    def test_preamble_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(server, "SYSTEM_PREAMBLE", "")
        assert server._compose_prompt([], "hello") == "User: hello"

    def test_history_is_injected_in_order(self, monkeypatch):
        monkeypatch.setattr(server, "SYSTEM_PREAMBLE", "")
        prompt = server._compose_prompt(
            [{"role": "user", "text": "first"},
             {"role": "assistant", "text": "answer"}],
            "second")
        assert prompt.index("User: first") < prompt.index("Assistant: answer")
        assert prompt.index("Assistant: answer") < prompt.index("User: second")

    def test_blank_history_turns_are_dropped(self, monkeypatch):
        monkeypatch.setattr(server, "SYSTEM_PREAMBLE", "")
        prompt = server._compose_prompt(
            [{"role": "user", "text": "   "}, {"role": "assistant", "text": ""}],
            "hello")
        assert "Conversation so far" not in prompt

    def test_agent_mode_adds_the_directive(self, monkeypatch):
        monkeypatch.setattr(server, "SYSTEM_PREAMBLE", "")
        monkeypatch.setattr(server, "AGENT_DIRECTIVE", "DIRECTIVE")
        assert "DIRECTIVE" in server._compose_prompt([], "go", agent_mode=True)
        assert "DIRECTIVE" not in server._compose_prompt([], "go", agent_mode=False)


    def test_a_compact_anchor_is_labelled_not_attributed(self, monkeypatch):
        """A /compact anchor is the conversation's own older turns folded up,
        not something either side said. Rendered as "Assistant: <notes>" the
        model reads its own summary as its previous reply and defends it."""
        monkeypatch.setattr(server, "SYSTEM_PREAMBLE", "")
        prompt = server._compose_prompt(
            [{"role": "compact", "text": "goal: ship TLS"},
             {"role": "user", "text": "carry on"}],
            "next")
        assert "[Summary of earlier conversation]\ngoal: ship TLS" in prompt
        assert "Assistant: goal: ship TLS" not in prompt
        assert "User: goal: ship TLS" not in prompt


class TestCompactPrompt:
    """The compaction prompt is a different shape from the chat prompt, and
    the differences are the point — each one is a way a compaction can quietly
    turn into an ordinary reply."""

    HISTORY = [{"role": "user", "text": "first"},
               {"role": "assistant", "text": "answer"}]

    def test_the_history_is_rendered_the_same_way_as_in_a_chat_turn(self):
        prompt = server._compose_compact_prompt(self.HISTORY)
        assert "User: first" in prompt
        assert "Assistant: answer" in prompt

    def test_it_carries_the_compaction_directive(self, monkeypatch):
        monkeypatch.setattr(server, "COMPACT_DIRECTIVE", "FOLD-IT-UP")
        assert "FOLD-IT-UP" in server._compose_compact_prompt(self.HISTORY)

    def test_it_has_no_reply_framing(self):
        assert "Now reply" not in server._compose_compact_prompt(self.HISTORY)

    def test_it_omits_the_system_preamble(self, monkeypatch):
        """The preamble tells the model to go inspect the filesystem. A
        summarizer has everything it needs in the prompt, and a nudge toward
        tools is the wrong one here."""
        monkeypatch.setattr(server, "SYSTEM_PREAMBLE", "GO-USE-YOUR-TOOLS")
        assert "GO-USE-YOUR-TOOLS" not in server._compose_compact_prompt(self.HISTORY)

    def test_a_focus_topic_asks_for_emphasis_not_exclusion(self):
        """A focus that licensed dropping the rest would lose the constraints
        the next turn still has to obey."""
        prompt = server._compose_compact_prompt(self.HISTORY, "the TLS work")
        assert "the TLS work" in prompt
        assert "without omitting" in prompt

    def test_a_blank_focus_adds_nothing(self):
        assert (server._compose_compact_prompt(self.HISTORY)
                == server._compose_compact_prompt(self.HISTORY, ""))


class TestTodoState:
    """The `todo` tool's result, which is what the plan panel actually tracks.

    It is read from the RESULT rather than the call: a call can be a partial
    merge (`{"merge": true, "todos": [{"id": "a", "status": "completed"}]}`)
    that says nothing about the other items, while the result always carries
    the whole list.
    """

    FULL = ('{"todos": [{"id": "alpha", "content": "do alpha", "status": "completed"}, '
            '{"id": "beta", "content": "do beta", "status": "in_progress"}, '
            '{"id": "gamma", "content": "do gamma", "status": "pending"}], '
            '"summary": {"total": 3}}')

    def test_reads_the_whole_list(self):
        state = server._todo_state(self.FULL)
        assert [t["text"] for t in state["todos"]] == ["do alpha", "do beta", "do gamma"]

    def test_maps_the_statuses_the_tool_actually_emits(self):
        state = server._todo_state(self.FULL)
        assert [t["status"] for t in state["todos"]] == ["done", "doing", "pending"]
        assert state["summary"] == {"total": 3, "done": 1, "doing": 1, "pending": 1}

    def test_a_truncated_payload_yields_nothing(self):
        """The display copy of a tool result is cut to 300 chars, which lands
        mid-JSON on any real list. That is the whole reason this is emitted as
        its own event instead of being scraped off the transcript line."""
        assert server._todo_state(self.FULL[:120]) is None

    def test_an_ordinary_tool_result_is_not_mistaken_for_a_plan(self):
        assert server._todo_state('{"output": "hello", "exit_code": 0}') is None
        assert server._todo_state("ok, done") is None
        assert server._todo_state("") is None

    def test_an_empty_list_is_not_a_plan(self):
        assert server._todo_state('{"todos": []}') is None

    def test_an_unexpected_item_shape_yields_no_panel_rather_than_a_wrong_one(self):
        assert server._todo_state('{"todos": ["alpha", "beta"]}') is None

    def test_a_label_falls_back_when_content_is_missing(self):
        state = server._todo_state('{"todos": [{"id": "a", "status": "pending"}, '
                                   '{"id": "b", "text": "written", "status": "pending"}]}')
        assert [t["text"] for t in state["todos"]] == ["a", "written"]

    def test_labels_are_redacted_like_any_other_agent_output(self):
        state = server._todo_state(
            '{"todos": [{"id": "1", "content": "use GITHUB_TOKEN=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", '
            '"status": "pending"}, {"id": "2", "content": "second", "status": "pending"}]}')
        assert "ghp_AAAA" not in state["todos"][0]["text"]


class TestDefaultPreamble:
    """The built-in note must stay installation-agnostic.

    It once hardcoded one machine's vault path. Every deployment inherited it,
    and the model — told a directory existed that did not — burned turns
    reconciling the missing path instead of running a tool. An asserted path
    outranks what the tools report, so a wrong one is worse than none.
    """

    def test_names_no_filesystem_path(self):
        assert "/" not in server._DEFAULT_PREAMBLE

    def test_names_no_specific_installation(self):
        lowered = server._DEFAULT_PREAMBLE.lower()
        for term in ("opser", "vault", "obsidian", "/host", "/mnt"):
            assert term not in lowered

    def test_still_tells_the_model_to_use_its_tools(self):
        """The whole reason the note exists — do not lose it while trimming."""
        assert "tools" in server._DEFAULT_PREAMBLE.lower()

    def test_can_still_be_overridden_per_install(self, monkeypatch):
        monkeypatch.setattr(server, "SYSTEM_PREAMBLE", "vault is at /host/X")
        assert "/host/X" in server._compose_prompt([], "hi")


class TestPreambleStripping:
    def test_the_injected_note_is_removed_from_a_transcript(self):
        raw = (f"{server.PREAMBLE_OPEN}internal note{server.PREAMBLE_CLOSE}\n"
               "what the user actually typed")
        assert (server.PREAMBLE_BLOCK_RE.sub("", raw).strip()
                == "what the user actually typed")

    def test_ordinary_text_is_untouched(self):
        text = "a normal message with < brackets > and $dollars"
        assert server.PREAMBLE_BLOCK_RE.sub("", text) == text


class TestMcpTestOutput:
    CONNECTED = """
Testing server 'kanban'
  Transport: stdio
  ✓ Connected (312 ms)
  ✓ Tools discovered: 2
    manage_task          Create, update, move or delete a task...
    list_boards          List every board
"""

    def test_parses_a_healthy_server(self):
        result = server._parse_mcp_test(self.CONNECTED)
        assert result["connected"] is True
        assert result["connect_ms"] == 312
        assert result["tool_count"] == 2
        assert [t["name"] for t in result["tools"]] == ["manage_task", "list_boards"]

    def test_parses_a_connection_failure(self):
        result = server._parse_mcp_test(
            "  ✗ Connection failed (5001 ms): Connection closed\n")
        assert result["connected"] is False
        assert result["connect_ms"] == 5001
        assert "Connection closed" in result["error"]

    def test_header_lines_are_not_mistaken_for_tools(self):
        """`  Transport:` and `  Auth:` sit above the banner and use a
        shallower indent — neither may show up as a discovered tool."""
        names = [t["name"] for t in server._parse_mcp_test(self.CONNECTED)["tools"]]
        assert "Transport" not in names
        assert "Auth" not in names

    def test_tool_count_falls_back_to_the_rows_seen(self):
        result = server._parse_mcp_test(
            "  ✓ Tools discovered: 0\n    only_tool          does a thing\n")
        assert result["tool_count"] == 1

    def test_empty_output_is_not_a_crash(self):
        assert server._parse_mcp_test("")["connected"] is False


class TestSkillsTable:
    TABLE = """
┏━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━┳━━━━━━━┳━━━━━━━━━┓
┃ Name      ┃ Category ┃ Source ┃ Trust ┃ Status  ┃
┡━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━╇━━━━━━━╇━━━━━━━━━┩
│ pdf       │ docs     │ bundled│ core  │ enabled │
│ xlsx      │          │ user   │ local │ enabled │
└───────────┴──────────┴────────┴───────┴─────────┘
"""

    def test_parses_rows(self):
        skills = server._parse_skills_table(self.TABLE)
        assert [s["name"] for s in skills] == ["pdf", "xlsx"]
        assert skills[0]["category"] == "docs"

    def test_header_row_is_skipped(self):
        assert all(s["name"] != "Name"
                   for s in server._parse_skills_table(self.TABLE))

    def test_missing_category_gets_a_placeholder(self):
        skills = server._parse_skills_table(self.TABLE)
        assert skills[1]["category"] == "uncategorised"

    def test_non_table_output_yields_nothing(self):
        assert server._parse_skills_table("no skills installed\n") == []


class TestStderrLog:
    LOG = """===== [12:00:00] starting MCP server 'alpha' =====
alpha line one
===== [12:00:01] starting MCP server 'beta' =====
beta traceback
===== [12:00:02] starting MCP server 'alpha' =====
alpha most recent failure
"""

    def test_returns_the_most_recent_block(self):
        assert server._last_stderr_block(self.LOG, "alpha") == "alpha most recent failure"

    def test_another_servers_output_does_not_bleed_in(self):
        assert "beta" not in server._last_stderr_block(self.LOG, "alpha")

    def test_unknown_server_returns_empty(self):
        assert server._last_stderr_block(self.LOG, "gamma") == ""


class TestToolFiltering:
    DISCOVERED = [{"name": "a"}, {"name": "b"}, {"name": "c"}]

    def test_no_filter_offers_everything(self):
        assert server._selected_tool_names({}, self.DISCOVERED) == ["a", "b", "c"]

    def test_include_narrows(self):
        cfg = {"tools": {"include": ["a", "c"]}}
        assert server._selected_tool_names(cfg, self.DISCOVERED) == ["a", "c"]

    def test_exclude_removes(self):
        cfg = {"tools": {"exclude": ["b"]}}
        assert server._selected_tool_names(cfg, self.DISCOVERED) == ["a", "c"]

    def test_exclude_wins_over_include(self):
        cfg = {"tools": {"include": ["a", "b"], "exclude": ["b"]}}
        assert server._selected_tool_names(cfg, self.DISCOVERED) == ["a"]


class TestRecycleSettings:
    def test_auto_reconnect_needs_a_limit(self):
        assert server._recycle_settings({})["auto_reconnect"] is False
        assert server._recycle_settings(
            {"idle_timeout_seconds": 60})["auto_reconnect"] is True

    def test_limits_are_read_from_the_lifecycle_block(self):
        cfg = {"lifecycle": {"max_lifetime_seconds": 900}}
        assert server._recycle_settings(cfg)["max_lifetime_seconds"] == 900

    def test_zero_and_garbage_count_as_unset(self):
        for value in (0, -1, "abc", None):
            settings = server._recycle_settings({"idle_timeout_seconds": value})
            assert settings["idle_timeout_seconds"] is None


class TestEnvRefs:
    def test_finds_both_placeholder_forms(self):
        cfg = {"env": {"A": "${TOKEN}"}, "headers": {"X": "Bearer ${env:KEY}"}}
        assert server._mcp_env_refs(cfg) == ["KEY", "TOKEN"]

    def test_deduplicates(self):
        cfg = {"env": {"A": "${T}", "B": "${T}"}}
        assert server._mcp_env_refs(cfg) == ["T"]

    def test_no_placeholders_is_empty(self):
        assert server._mcp_env_refs({"env": {"A": "literal"}}) == []


class TestToolSchemaFacts:
    def test_extracts_the_action_enum(self):
        entry = {"description": "manage a task", "inputSchema": {
            "properties": {"action": {"enum": ["create", "delete"]},
                           "id": {"type": "string"}},
            "required": ["action"]}}
        facts = server._tool_schema_facts(entry)
        assert facts["actions"] == ["create", "delete"]
        assert facts["required_params"] == ["action"]
        assert facts["params"] == ["action", "id"]

    def test_alternative_enum_key_is_found(self):
        entry = {"inputSchema": {"properties": {"operation": {"enum": ["read"]}}}}
        assert server._tool_schema_facts(entry)["actions"] == ["read"]

    def test_a_schemaless_tool_is_handled(self):
        facts = server._tool_schema_facts({"description": "plain"})
        assert facts == {"full_description": "plain", "actions": [],
                         "required_params": [], "params": []}


class TestStatusLabel:
    def test_names_the_running_tool(self):
        label = server._status_label([{"kind": "call", "name": "read_file"}])
        assert label == "Running tool: read_file"

    def test_reports_the_latest_event(self):
        events = [{"kind": "call", "name": "old"}, {"kind": "chunk"}]
        assert server._status_label(events) == "Generating response…"

    def test_a_model_switch_is_surfaced(self):
        label = server._status_label([{"kind": "model_switch", "to": "gemini"}])
        assert "gemini" in label

    def test_no_events_yet(self):
        assert server._status_label([]) == "Processing prompt…"


class TestSessionIdValidation:
    def test_accepts_a_hermes_id(self):
        assert server._valid_sid("20260715_193102_62eba9")

    def test_rejects_a_path(self):
        assert not server._valid_sid("../../etc/passwd")

    def test_rejects_empty(self):
        assert not server._valid_sid("")
