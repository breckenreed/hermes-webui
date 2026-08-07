"""
Hermes WebUI — a minimal "claude-code style" web interface to command and
interact with a local Hermes agent container.

Architecture
------------
The browser talks to this FastAPI backend. The backend drives the Hermes
agent by running its one-shot CLI inside the already-running Hermes
container:

    docker exec hermes-agent hermes -z "<prompt>" --resume <session> --yolo --cli

stdout is streamed back to the browser over Server-Sent Events (SSE) so the
chat feels live. Session continuity is handled entirely by Hermes: we pass a
stable --resume key per conversation and Hermes keeps the history in its own
SQLite session store.

This container needs the Docker socket mounted (see docker-compose.yml) so it
can exec into the Hermes container.
"""
import asyncio
import hmac
import json
import os
import re
import shlex
import shutil
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from pydantic import BaseModel

HERMES_CONTAINER = os.environ.get("HERMES_CONTAINER", "hermes-agent")
DOCKER_BIN = os.environ.get("DOCKER_BIN", "docker")
LLM_CLIENT_UID = os.environ.get("LLM_CLIENT_UID", "")
DEFAULT_MODEL = os.environ.get("HERMES_MODEL", "")  # optional override
STATIC_DIR = Path(__file__).parent / "static"

# A short context note prepended to every prompt. Small local models tend to
# hallucinate their environment ("I'm a WSL instance…") and answer filesystem
# questions without actually running a tool. This nudges them to act. Override
# with HERMES_SYSTEM_PREAMBLE; set it to an empty string to disable entirely.
_DEFAULT_PREAMBLE = (
    "You are running inside a Linux container (not WSL). The user's Obsidian "
    "vault is bind-mounted read-write at /host/MyVault. Always use your "
    "tools to inspect or modify the filesystem — never guess about your "
    "environment or where files live."
)
SYSTEM_PREAMBLE = os.environ.get("HERMES_SYSTEM_PREAMBLE", _DEFAULT_PREAMBLE).strip()

# Completion pressure for "Agent mode". hermes -z is one-shot: within a single
# run the agent has many tool iterations, but weaker local models tend to make
# a plan / todo list and then STOP to explain instead of executing it. This
# directive tells the model to carry the whole plan through in that one run.
# It changes nothing about safety (tools already run without confirmation under
# --yolo) and spawns no extra sessions — the same single process just does the
# work and exits when the plan is done. Override with HERMES_AGENT_DIRECTIVE.
_DEFAULT_AGENT_DIRECTIVE = (
    "AUTONOMOUS EXECUTION MODE. Your tools run without confirmation. Work "
    "through the user's ENTIRE request: if you make a plan or todo list, "
    "immediately EXECUTE every item yourself with your tools and keep going "
    "until all steps are actually done. Do not stop to merely explain what you "
    "will do next — do it. Prefer taking the next action over narrating it.\n"
    "COMPLETION SIGNAL: end your reply with the exact token [[TASK_COMPLETE]] "
    "on its own line ONLY when the whole task is fully finished. If any step "
    "remains, do NOT emit that token — the system will prompt you to continue."
)
AGENT_DIRECTIVE = os.environ.get("HERMES_AGENT_DIRECTIVE", _DEFAULT_AGENT_DIRECTIVE).strip()

# Markers wrap the preamble in the sent prompt so we can strip it back out when
# rendering a stored transcript — the user only ever sees their own text.
PREAMBLE_OPEN = "<<webui-context>>"
PREAMBLE_CLOSE = "<</webui-context>>"
PREAMBLE_BLOCK_RE = re.compile(
    re.escape(PREAMBLE_OPEN) + r".*?" + re.escape(PREAMBLE_CLOSE) + r"\s*",
    re.DOTALL,
)

app = FastAPI(title="Hermes WebUI")

# ── Access control ───────────────────────────────────────────────────────
# The webui grants full agent access (yolo file writes included) to whoever
# reaches the port, so on a semi-public LAN it must not be open. When
# WEBUI_TOKEN is set, every /api/* request needs `Authorization: Bearer
# <token>`; the page shell itself stays public (it holds no data — it shows a
# lock screen until the token is entered). Empty token = open access, for
# localhost-only setups.
WEBUI_TOKEN = os.environ.get("WEBUI_TOKEN", "").strip()


@app.middleware("http")
async def require_token(request: Request, call_next):
    if WEBUI_TOKEN and request.url.path.startswith("/api/"):
        supplied = request.headers.get("authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[7:]
        if not hmac.compare_digest(supplied, WEBUI_TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)

# Matches Hermes session IDs like 20260715_193102_62eba9
SESSION_ID_RE = re.compile(r"\d{8}_\d{6}_[0-9a-f]{6}")
# Strip ANSI escape sequences that the CLI may emit
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _docker(*args: str) -> list[str]:
    return [DOCKER_BIN, *args]


def _exec_prefix(extra_env: dict | None = None) -> list[str]:
    """docker exec into the Hermes container, passing the LLM key through.

    `extra_env` adds more `-e` flags — used to widen Rich's table output (see
    /api/skills), which otherwise wraps to an 80-column default and truncates
    the very values we're trying to read.
    """
    cmd = _docker("exec", "-i")
    if LLM_CLIENT_UID:
        cmd += ["-e", f"LLM_CLIENT_UID={LLM_CLIENT_UID}"]
    for key, value in (extra_env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += [HERMES_CONTAINER]
    return cmd


class ChatBody(BaseModel):
    message: str
    session: str                      # unique per-turn key (isolation + stop handle)
    history: list[dict] = []          # prior [{role, text}] turns, injected as context
    agent_mode: bool = False          # add completion pressure for multi-step / todo work
    model: str = ""                   # per-turn model override (blank = Hermes' configured default)
    provider: str = ""                # per-turn provider override, paired with `model`


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health():
    """Report whether docker + the Hermes container are reachable."""
    ok = False
    detail = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *_docker("inspect", "-f", "{{.State.Running}}", HERMES_CONTAINER),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        detail = (out or err).decode(errors="replace").strip()
        ok = detail == "true"
    except Exception as e:  # noqa: BLE001
        detail = str(e)
    return {
        "ok": ok,
        "container": HERMES_CONTAINER,
        "running": detail,
        "docker_available": shutil.which(DOCKER_BIN) is not None or DOCKER_BIN == "docker",
    }


@app.get("/api/sessions")
async def sessions(limit: int = 40):
    """List recent Hermes sessions for the sidebar."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *_exec_prefix(), "hermes", "sessions", "list", "--source", "cli",
            "--limit", str(limit),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "sessions": []}, status_code=500)

    text = ANSI_RE.sub("", (out or b"").decode(errors="replace"))
    items = []
    for line in text.splitlines():
        m = SESSION_ID_RE.search(line)
        if not m:
            continue
        sid = m.group(0)
        left = line[: m.start()].rstrip()
        # Columns separated by runs of 2+ spaces:
        # Preview | Workspace | LastActive | Src
        cols = [c.strip() for c in re.split(r"\s{2,}", left) if c.strip()]
        title = cols[0] if cols else ""
        # Strip any leftover webui context note from the preview text.
        title = PREAMBLE_BLOCK_RE.sub("", title).strip()
        if title.startswith(PREAMBLE_OPEN):  # truncated marker in the preview
            title = ""
        if title in ("—", "-", ""):
            title = ""
        # Last-active is the column that looks like a time, e.g. "3m ago".
        last_active = ""
        for c in cols[1:]:
            if c == "just now" or re.search(r"\b(ago|now)\b", c):
                last_active = c
                break
        items.append({"id": sid, "title": title, "last_active": last_active})
    return {"sessions": items}


@app.get("/api/session/{sid}")
async def session_transcript(sid: str):
    """Return the normalized message history for one session (for loading in the UI)."""
    if not SESSION_ID_RE.fullmatch(sid) and not re.fullmatch(r"[\w.-]{1,80}", sid):
        return JSONResponse({"error": "bad session id"}, status_code=400)
    try:
        proc = await asyncio.create_subprocess_exec(
            *_exec_prefix(), "hermes", "sessions", "export",
            "--format", "jsonl", "--session-id", sid, "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "messages": []}, status_code=500)

    text = (out or b"").decode(errors="replace").strip()
    if not text:
        return {"messages": [], "note": (err or b"").decode(errors="replace")[:400]}

    messages = []
    try:
        # Export is a single JSON object whose "messages" holds the transcript.
        obj = json.loads(text.splitlines()[0])
        raw_msgs = obj.get("messages", [])
    except Exception:  # noqa: BLE001
        raw_msgs = []

    for m in raw_msgs:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        content = (content or "").strip()
        tools = []
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name") or ""
            args = fn.get("arguments")
            if isinstance(args, (dict, list)):
                args = json.dumps(args)
            tools.append(f"{name} {str(args or '')[:160]}".strip())

        if role == "user" and content:
            # Hide the webui context preamble we prepend to prompts.
            content = PREAMBLE_BLOCK_RE.sub("", content).strip()
            if content:
                messages.append({"role": "user", "text": content, "tools": []})
        elif role == "assistant":
            if content or tools:
                messages.append({"role": "assistant", "text": content, "tools": tools})
        elif role == "tool":
            # attach the tool result as a compact line on the previous assistant msg
            snippet = content[:200].replace("\n", " ")
            if messages and messages[-1]["role"] == "assistant":
                messages[-1]["tools"].append(f"↳ {snippet}")
    return {"messages": messages}


def _valid_sid(sid: str) -> bool:
    return bool(SESSION_ID_RE.fullmatch(sid) or re.fullmatch(r"[\w.-]{1,80}", sid))


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, ANSI_RE.sub("", (out or b"").decode(errors="replace")).strip()


@app.delete("/api/session/{sid}")
async def delete_session(sid: str):
    if not _valid_sid(sid):
        return JSONResponse({"error": "bad session id"}, status_code=400)
    try:
        code, out = await _run(*_exec_prefix(), "hermes", "sessions", "delete", sid, "--yes")
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": code == 0, "message": out}, status_code=200 if code == 0 else 500)


class RenameBody(BaseModel):
    title: str


@app.post("/api/session/{sid}/rename")
async def rename_session(sid: str, body: RenameBody):
    title = body.title.strip()
    if not _valid_sid(sid):
        return JSONResponse({"error": "bad session id"}, status_code=400)
    if not title:
        return JSONResponse({"error": "empty title"}, status_code=400)
    try:
        code, out = await _run(*_exec_prefix(), "hermes", "sessions", "rename", sid, title)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": code == 0, "message": out}, status_code=200 if code == 0 else 500)


def _compose_prompt(history: list[dict], message: str, agent_mode: bool = False) -> str:
    """Build a single prompt carrying the whole conversation.

    `hermes -z` is one-shot: each call forks a fresh session and does NOT
    reliably carry prior turns forward. So the webui owns the conversation and
    injects the full history into every prompt — that gives the model correct,
    explicit context regardless of Hermes' session store. The system preamble
    (if any) rides at the top, followed by the agent directive when Agent mode
    is on.
    """
    parts: list[str] = []
    if SYSTEM_PREAMBLE:
        parts.append(SYSTEM_PREAMBLE)
    if agent_mode and AGENT_DIRECTIVE:
        parts.append(AGENT_DIRECTIVE)
    turns = [m for m in (history or []) if (m.get("text") or "").strip()]
    if turns:
        parts.append("# Conversation so far")
        for m in turns:
            who = "User" if m.get("role") == "user" else "Assistant"
            parts.append(f"{who}: {m['text'].strip()}")
        parts.append(
            "# Now reply to the latest user message below, using the "
            "conversation above as context."
        )
    parts.append(f"User: {message.strip()}")
    return "\n\n".join(parts)


# In-flight chat processes, keyed by session, so the UI can stop them.
RUNNING: dict[str, asyncio.subprocess.Process] = {}

# Strong references to background tasks (asyncio keeps only weak ones — a
# task could otherwise be garbage-collected mid-run after its parent scope,
# e.g. a cancelled SSE generator, goes away).
BG_TASKS: set = set()


def _spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)
    return t

# ── Turn records ─────────────────────────────────────────────────────────
# The server is the source of truth for a turn's progress; the SSE stream is
# just a live view. Mobile clients drop constantly (locked phone, backgrounded
# browser, flaky wifi), so every event is recorded here and mirrored to disk —
# a reconnecting client (or a restarted webui) replays the record instead of
# losing the reply. Records: {status: running|done|failed, events, text, code,
# turn_id, ts}.
TURNS: dict[str, dict] = {}
_TURNS_MAX = 60
TURNS_DIR = Path(os.environ.get("TURNS_DIR", "/tmp/hermes-webui-turns"))
try:
    TURNS_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # noqa: BLE001
    pass
TURN_KEY_RE = re.compile(r"[\w.-]{1,120}")


def _persist_turn(key: str, rec: dict) -> None:
    try:
        (TURNS_DIR / f"{key}.json").write_text(json.dumps(rec), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _load_turn(key: str) -> dict | None:
    try:
        return json.loads((TURNS_DIR / f"{key}.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _drop_turn(key: str) -> None:
    TURNS.pop(key, None)
    try:
        (TURNS_DIR / f"{key}.json").unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _trim_turns() -> None:
    while len(TURNS) > _TURNS_MAX:
        TURNS.pop(next(iter(TURNS)))


async def _kill_container_chat(session: str) -> None:
    """Terminate the hermes turn for `session` running *inside* the container.

    Killing the local `docker exec` client does not reliably stop the process
    it spawned in the container, so we pkill it by its unique `--resume <key>`
    command line. Session keys contain no regex metacharacters.
    """
    try:
        p = await asyncio.create_subprocess_exec(
            *_exec_prefix(), "pkill", "-TERM", "-f", "--", f"--resume {session}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await p.wait()
    except Exception:  # noqa: BLE001
        pass


async def _run_out(*args: str, timeout: float = 30) -> str:
    """Run a command and return stdout only (stderr discarded) — for JSON."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (out or b"").decode(errors="replace")


async def _latest_cli_session_id() -> str:
    """Native id of the most-recently-active CLI session in Hermes' store."""
    try:
        out = await _run_out(
            *_exec_prefix(), "hermes", "sessions", "list",
            "--source", "cli", "--limit", "1", timeout=20)
        m = SESSION_ID_RE.search(ANSI_RE.sub("", out))
        return m.group(0) if m else ""
    except Exception:  # noqa: BLE001
        return ""


async def _export_turn(sid: str) -> tuple[list[dict], str, str]:
    """Ordered activity of one Hermes session + its final reply text + the
    model that ended up serving it.

    Events mirror the agent's actions into the chat, Claude-code style:
      call    — a tool invocation (name + args)
      result  — what the tool returned
      interim — an intermediate assistant message (a finished sub-step),
                emitted only for messages that are NOT the last one, so the
                final reply (which arrives via stdout) is never duplicated.

    The returned model is the session's ACTUAL final model — if Hermes'
    built-in fallback chain (agent.chat_completion_helpers.try_activate_
    fallback) switched away from whatever was requested mid-turn (rate
    limit / billing / connection failure on the requested model), this is
    how the caller finds out, since that switch is otherwise silent: it's
    only a live-console status line, never written into the transcript.

    Read-only, so polling it mid-turn is safe.
    """
    try:
        out = await _run_out(
            *_exec_prefix(), "hermes", "sessions", "export",
            "--format", "jsonl", "--session-id", sid, "-", timeout=25)
        out = out.strip()
        obj = json.loads(out[out.index("{"):].splitlines()[0])
    except Exception:  # noqa: BLE001
        return [], "", ""
    final_model = str(obj.get("model") or "")
    msgs = obj.get("messages", [])
    events: list[dict] = []
    final_text = ""
    for i, m in enumerate(msgs):
        role = m.get("role")
        last = i == len(msgs) - 1
        content = m.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = (content or "").strip()
        if role == "assistant":
            for tc in (m.get("tool_calls") or []):
                fn = (tc or {}).get("function", {}) if isinstance(tc, dict) else {}
                args = fn.get("arguments")
                if isinstance(args, (dict, list)):
                    args = json.dumps(args, ensure_ascii=False)
                events.append({"kind": "call",
                               "name": fn.get("name") or "?",
                               "args": str(args or "")[:300]})
            if content:
                if last:
                    final_text = content
                else:
                    events.append({"kind": "interim", "text": content[:2000]})
        elif role == "tool":
            events.append({"kind": "result",
                           "text": content.replace("\n", " ")[:300]})
    return events, final_text, final_model


async def _stream_chat(history: list[dict], message: str, session: str,
                       agent_mode: bool = False, model: str = "", provider: str = ""):
    """Run the Hermes one-shot CLI and yield SSE events as output arrives.

    `session` is a unique per-turn key: it isolates this turn's Hermes session
    (a fresh name starts clean) and is the handle /api/stop uses to kill it.
    Context comes from the injected history, not from Hermes' session store.

    `model`/`provider` override Hermes' configured default for THIS turn only
    (plain `-m`/`--provider` CLI flags — no config.yaml change, no restart).
    This is how the webui's online-model picker routes a turn to Gemini (or
    any other `fallback_providers` entry) on demand, rather than only via
    Hermes' automatic failover-on-error path.

    While the turn runs, a poller watches the forked Hermes session and emits
    `tool` events (calls + results) so the UI can show the agent's actions
    live, Claude-code style.
    """
    args = _exec_prefix() + [
        "hermes", "-z", _compose_prompt(history, message, agent_mode),
        "--resume", session,
        "--yolo", "--cli",
    ]
    requested_model = model or DEFAULT_MODEL   # for mid-turn switch detection below
    if requested_model:
        args += ["-m", requested_model]
    if provider:
        args += ["--provider", provider]

    def sse(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

    yield sse("start", {"session": session})

    # Snapshot the newest session id so the poller can spot this turn's fork.
    pre_id = await _latest_cli_session_id()

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:  # noqa: BLE001
        yield sse("error", {"message": f"failed to launch hermes: {e}"})
        yield sse("done", {})
        return

    import time

    RUNNING[session] = proc
    captured: list[str] = []
    tasks: dict = {}
    q: asyncio.Queue = asyncio.Queue()
    rec = {"status": "running", "events": [], "text": "", "code": None,
           "turn_id": "", "pre_id": pre_id, "ts0": time.time(), "ts": time.time()}
    TURNS[session] = rec
    _trim_turns()
    _persist_turn(session, rec)

    def record(kind: str, data: dict) -> dict:
        ev = {"kind": kind, "ts": time.time(), **data}
        rec["events"].append(ev)
        rec["ts"] = ev["ts"]
        return ev

    async def finish_record(rc: int) -> None:
        """Mark the record done; runs whether or not a client is attached."""
        rec["status"] = "done"
        rec["code"] = rc
        rec["text"] = "\n".join(captured)
        # Late tool/interim events the 3s poller didn't catch yet.
        try:
            if not rec["turn_id"]:
                nid = await _latest_cli_session_id()
                if nid and nid != pre_id:
                    rec["turn_id"] = nid
            if rec["turn_id"]:
                events, final_text, final_model = await _export_turn(rec["turn_id"])
                known = sum(1 for e in rec["events"] if e.get("kind") != "chunk")
                for ev in events[known:]:
                    ev.setdefault("ts", time.time())
                    rec["events"].append(ev)
                if not rec["text"] and final_text:
                    rec["text"] = final_text
                # Hermes' own fallback chain (try_activate_fallback) swaps
                # models mid-turn on rate-limit/billing/connection failure
                # SILENTLY — the "⚠️ Rate limited — switching..." line is a
                # live-console status only, never written to the session. The
                # session's final `model` field is the only durable signal a
                # switch happened, so diff it against what we asked for and
                # surface it as its own event the client renders as a notice.
                if requested_model and final_model:
                    req = requested_model.strip().lower()
                    fin = final_model.strip().lower()
                    if req and fin and req != fin:
                        switch_ev = record("model_switch", {"from": requested_model, "to": final_model})
                        await q.put(("model_switch", switch_ev))
        except Exception:  # noqa: BLE001
            pass
        _persist_turn(session, rec)

    async def read_stdout():
        """Drain stdout to the queue + record. Runs to completion even if the
        client detaches, then finalizes the record for /api/turn recovery."""
        try:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = ANSI_RE.sub("", raw.decode(errors="replace")).rstrip("\n")
                captured.append(line)
                await q.put(("chunk", record("chunk", {"text": line})))
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                rc = await proc.wait()
            except Exception:  # noqa: BLE001
                rc = -1
            if RUNNING.get(session) is proc:
                del RUNNING[session]
            # The poller outlives a detached client on purpose; stop it only
            # now that the turn is over and the record is being finalized.
            t = tasks.get("poller")
            if t:
                t.cancel()
            await finish_record(rc)
            await q.put(("eof", {"code": rc}))

    async def poll_tools():
        """Watch this turn's forked Hermes session; queue + record new events
        (tool calls, tool results, and finished sub-step interim messages)."""
        sent = 0
        while True:
            await asyncio.sleep(3)
            try:
                if not rec["turn_id"]:
                    nid = await _latest_cli_session_id()
                    if not nid or nid == pre_id:
                        continue
                    rec["turn_id"] = nid
                    _persist_turn(session, rec)
                events, _, _ = await _export_turn(rec["turn_id"])
                for ev in events[sent:]:
                    ev.setdefault("ts", time.time())
                    rec["events"].append(ev)
                    await q.put(("tool", ev))
                if len(events) > sent:
                    sent = len(events)
                    rec["ts"] = time.time()
                    _persist_turn(session, rec)
            except Exception:  # noqa: BLE001
                pass

    reader = _spawn(read_stdout())
    poller = _spawn(poll_tools())
    tasks["poller"] = poller
    try:
        rc = 0
        streamed_nonchunk = 0
        while True:
            try:
                kind, data = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                # Heartbeat: long turns can be silent for many minutes (the
                # model is thinking). Pings keep the client's stall-watchdog
                # fed, and writing into a dead socket makes the server notice
                # a vanished client instead of holding the stream open.
                yield sse("ping", {})
                continue
            if kind == "eof":
                rc = data.get("code", 0)
                break
            if kind in ("tool", "model_switch"):
                streamed_nonchunk += 1
            yield sse(kind, data)
        poller.cancel()
        # Replay events finish_record added after the live stream ended
        # (turns faster than the poll interval), each under its own SSE event
        # name — call/result/interim ride the "tool" event per the existing
        # frontend contract; model_switch (and anything else) uses its own kind.
        non_chunk = [e for e in rec["events"] if e.get("kind") != "chunk"]
        for ev in non_chunk[streamed_nonchunk:]:
            sse_name = "tool" if ev.get("kind") in ("call", "result", "interim") else ev.get("kind", "tool")
            yield sse(sse_name, ev)
        yield sse("done", {"code": rc, "stopped": False})
    except asyncio.CancelledError:
        # Client dropped (phone locked / backgrounded / wifi blip). Do NOT
        # kill the turn and do NOT stop the poller: both keep running so the
        # record accumulates sub-step progress for /api/turn reattachment.
        # read_stdout cancels the poller when the process finishes.
        # Deliberate stops go through /api/stop.
        raise


# Context-window info is expensive to compute (spawns hermes), so cache it.
CONTEXT_CACHE: dict = {"ts": 0.0, "data": None}


@app.get("/api/context")
async def context_info():
    """Context-window report: model, configured context length, and the fixed
    prompt budget (system prompt + skills + memory + tool schemas) that Hermes
    spends before the conversation even starts. Token counts are estimated at
    ~4 chars/token. Cached for 5 minutes."""
    import time

    now = time.time()
    if CONTEXT_CACHE["data"] and now - CONTEXT_CACHE["ts"] < 300:
        return CONTEXT_CACHE["data"]

    model, ctx_len = "", 0
    try:
        _, out = await asyncio.wait_for(
            _run(*_exec_prefix(), "cat", "/opt/data/config.yaml"), timeout=15)
        m = re.search(r"^\s*default:\s*(\S+)", out, re.M)
        if m:
            model = m.group(1)
        m = re.search(r"^\s*context_length:\s*(\d+)", out, re.M)
        if m:
            ctx_len = int(m.group(1))
    except Exception:  # noqa: BLE001
        pass

    base_tokens, breakdown = 0, {}
    try:
        _, out = await asyncio.wait_for(
            _run(*_exec_prefix(), "hermes", "prompt-size", "--json"), timeout=60)
        j = json.loads(out[out.index("{"):])
        chars = sum(
            (j.get(k) or {}).get("chars", 0)
            for k in ("system_prompt", "skills_index", "memory", "user_profile"))
        tool_bytes = (j.get("tools") or {}).get("json_bytes", 0)
        base_tokens = round((chars + tool_bytes) / 4)
        breakdown = {
            "system_prompt_chars": (j.get("system_prompt") or {}).get("chars", 0),
            "skills_index_chars": (j.get("skills_index") or {}).get("chars", 0),
            "tools_json_bytes": tool_bytes,
            "tool_count": (j.get("tools") or {}).get("count", 0),
        }
        if not model:
            model = j.get("model", "")
    except Exception:  # noqa: BLE001
        pass

    data = {"model": model, "context_length": ctx_len,
            "base_tokens": base_tokens, "breakdown": breakdown}
    CONTEXT_CACHE.update(ts=now, data=data)
    return data


# Model list is cheap (one `cat`, no hermes invocation) but still worth caching
# briefly — the composer's model picker fetches it on every conversation open.
MODELS_CACHE: dict = {"ts": 0.0, "data": None}


@app.get("/api/models")
async def models_info():
    """Selectable models for the composer's model picker: Hermes' configured
    primary (local) model, plus every entry in `fallback_providers`.

    fallback_providers is normally an AUTOMATIC failover chain (tried only
    when the primary errors with a rate-limit/5xx/connection failure) — this
    endpoint repurposes the same config block as a menu of on-demand options,
    so picking one and sending a turn routes THAT turn's `hermes -z` call
    through `-m <model> --provider <provider>` instead of waiting for the
    primary to fail first. Cached 5 minutes.
    """
    import time

    now = time.time()
    if MODELS_CACHE["data"] and now - MODELS_CACHE["ts"] < 300:
        return MODELS_CACHE["data"]

    primary = {"model": "", "provider": ""}
    options: list[dict] = []
    try:
        _, out = await asyncio.wait_for(
            _run(*_exec_prefix(), "cat", "/opt/data/config.yaml"), timeout=15)
        cfg = yaml.safe_load(out) or {}
        m = cfg.get("model") or {}
        primary = {"model": str(m.get("default") or ""), "provider": str(m.get("provider") or "")}

        raw = cfg.get("fallback_providers")
        entries = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        for e in entries:
            if not isinstance(e, dict):
                continue
            model_id = str(e.get("model") or "").strip()
            provider_id = str(e.get("provider") or "").strip()
            # Mirrors Hermes' own get_fallback_chain(): an entry missing
            # either key is invalid and never gets tried, so don't offer it.
            if model_id and provider_id:
                # context_length/max_tokens ride along so the webui's context
                # meter can size its denominator to whichever model is
                # actually selected — an online model's window (e.g. Gemini's
                # 1M) has nothing to do with the local primary's.
                try:
                    ctx_len = int(e.get("context_length") or 0)
                except (TypeError, ValueError):
                    ctx_len = 0
                try:
                    max_tok = int(e.get("max_tokens") or 0)
                except (TypeError, ValueError):
                    max_tok = 0
                options.append({
                    "model": model_id, "provider": provider_id,
                    "context_length": ctx_len, "max_tokens": max_tok,
                })
    except Exception:  # noqa: BLE001
        pass

    data = {"primary": primary, "options": options}
    MODELS_CACHE.update(ts=now, data=data)
    return data


# ── MCP servers ──────────────────────────────────────────────────────────
# Hermes reads MCP servers from `mcp_servers` in config.yaml and registers each
# server's tools into the agent as `mcp__<server>__<tool>` (see
# tools/mcp_tool.py: mcp_prefixed_tool_name). Neither `hermes mcp list` nor
# `hermes mcp test` has a --json mode and BOTH exit 0 even when a server is
# dead, so health can't be taken from a return code — the report below reads
# the config directly (same `cat` trick as /api/models) and parses the human
# output of `hermes mcp test` for liveness + tool discovery.

# `${VAR}` / `${env:VAR}` — the two placeholder forms Hermes' _interpolate_env_
# vars() resolves. An UNRESOLVED placeholder is the signal we care about most:
# Hermes leaves it literal rather than erroring, so the server still launches
# and still advertises its tools, and the failure only lands later as a 401 on
# the first real API call. That's the difference between "server is down" and
# "server is up but has no usable credentials", which the UI reports separately.
MCP_ENV_REF_RE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")

MCP_CONNECTED_RE = re.compile(r"✓\s*Connected\s*\((\d+)\s*ms\)")
MCP_FAILED_RE = re.compile(r"✗\s*Connection failed\s*\((\d+)\s*ms\)\s*:?\s*(.*)")
MCP_TOOLCOUNT_RE = re.compile(r"✓\s*Tools discovered:\s*(\d+)")
# Tool rows are indented ~4 spaces; the "  Transport:" / "  Auth:" headers use
# 2 and carry a colon, so a 3+ space indent plus a bare identifier excludes
# them. Long descriptions can wrap to column 0 — those lines fail the indent
# test and are skipped rather than being mistaken for another tool.
MCP_TOOL_LINE_RE = re.compile(r"^\s{3,}([A-Za-z_][A-Za-z0-9_.-]*)\s{2,}(\S.*?)\s*$")

# Substrings that mean "the server answered, but rejected our credentials" as
# opposed to "we never reached it". Checked against the connection error text.
MCP_AUTH_HINTS = (
    "401", "403", "unauthorized", "forbidden", "invalid token", "invalid api key",
    "authentication", "auth failed", "oauth", "api key", "apikey", "access denied",
    "missing required environment", "credential",
)

# A probe of what a Hermes process in the container can actually resolve:
# process env plus ~/.hermes/.env, which Hermes loads before interpolating (see
# _load_mcp_config). Only names with a NON-EMPTY value count — an empty value
# resolves the placeholder to "" and still breaks the server, so treating it as
# "configured" would report a false green.
_ENV_PROBE = r"""
import json, os
names = {k for k, v in os.environ.items() if str(v).strip()}
try:
    with open('/opt/data/.env', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = k.strip()
            if k.startswith('export '):
                k = k[7:].strip()
            if v.strip().strip('\'"'):
                names.add(k)
except Exception:
    pass
print(json.dumps(sorted(names)))
"""


async def _container_env_names() -> set[str] | None:
    """Env var names resolvable inside the container, with non-empty values.

    None (not an empty set) when the probe itself failed — the container is
    always going to have *some* env, so an empty result means we couldn't look
    rather than "nothing is configured". The caller must not report every
    credential as missing off the back of a failed probe.
    """
    try:
        out = await _run_out(*_exec_prefix(), "python3", "-c", _ENV_PROBE, timeout=20)
        names = set(json.loads(out[out.index("["):]))
        return names or None
    except Exception:  # noqa: BLE001
        return None


# Errors that mean "try again", not "this is broken". A stdio server that lost a
# startup race, or a network MCP that blipped, reports one of these; a genuinely
# misconfigured one reports the same thing every time. Retried once (see
# _probe_mcp_server) so a transient blip doesn't paint the panel red.
MCP_TRANSIENT_HINTS = (
    "connection closed", "timed out", "timeout", "broken pipe",
    "connection reset", "temporarily unavailable", "econnrefused",
)


async def _stdio_command_exists(cfg: dict) -> bool | None:
    """Can the container actually execute this server's stdio command?

    This is the check whose absence cost a day of debugging. Hermes wraps every
    POSIX stdio server in mcp_stdio_watchdog.py (tools/mcp_tool.py:
    _wrap_command_with_watchdog), so the wrapper — python, which always exists —
    is what gets spawned. When the REAL command is missing, the wrapper starts
    fine, dies of FileNotFoundError, and the MCP client sees nothing but a closed
    pipe. Hermes' own _format_connect_error() has a good "missing executable"
    message, but it can never fire here because no ENOENT ever reaches it: the
    user-visible result is a bare "Connection closed".

    Returns True/False, or None when the check itself couldn't run (don't
    downgrade a server's state off a failed probe).
    """
    command = str(cfg.get("command") or "").strip()
    if not command:
        return None
    try:
        # `command -v` resolves PATH and shell builtins the same way the spawn
        # will. An absolute path is checked as a file instead, since `command -v`
        # on a path only tests executability, which is the same question.
        code, _ = await asyncio.wait_for(
            _run(*_exec_prefix(), "sh", "-lc", f"command -v {shlex.quote(command)}"),
            timeout=20)
        return code == 0
    except Exception:  # noqa: BLE001
        return None


# Hermes writes every stdio server's stderr to ~/.hermes/logs/mcp-stderr.log,
# prefixing each launch with `===== [ts] starting MCP server '<name>' =====`
# (tools/mcp_tool.py: _write_stderr_log_header). That file is the ONLY place the
# real cause of a failed stdio launch exists — see _stdio_command_exists above.
MCP_STDERR_LOG = "/opt/data/logs/mcp-stderr.log"
_MCP_STDERR_HEADER_RE = re.compile(
    r"^=+\s*\[(?P<ts>[^\]]+)\]\s*starting MCP server '(?P<name>[^']+)'\s*=+\s*$")


def _last_stderr_block(log: str, name: str, max_lines: int = 14) -> str:
    """Last launch block for `name` from mcp-stderr.log, trimmed to max_lines.

    Blocks run from one header to the next header for ANY server, so a busy
    multi-server log can't bleed another server's output into this one.
    """
    lines = log.splitlines()
    start = -1
    for i, line in enumerate(lines):
        m = _MCP_STDERR_HEADER_RE.match(line.strip())
        if m and m.group("name") == name:
            start = i
    if start < 0:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _MCP_STDERR_HEADER_RE.match(lines[j].strip()):
            end = j
            break
    body = [ln for ln in lines[start + 1:end] if ln.strip()]
    if not body:
        return ""
    # The tail carries the exception; the head is usually framework noise.
    return "\n".join(body[-max_lines:])


async def _mcp_stderr_tail(name: str) -> str:
    try:
        out = await _run_out(*_exec_prefix(), "tail", "-n", "400", MCP_STDERR_LOG, timeout=20)
        return _last_stderr_block(out, name)
    except Exception:  # noqa: BLE001
        return ""


# Hermes caches each connected server's real tool schemas here (written by
# tools/mcp_schema_cache.py at connect time). This is the ONLY source of full
# tool metadata available to the webui: `hermes mcp test` renders a console
# table whose descriptions it truncates to 55 chars (hermes_cli/mcp_config.py:
# cmd_mcp_test), after _probe_single_server has already cut them to 80 — so a
# description parsed from that output is a fragment with no way to recover the
# rest. The cache holds the untruncated text plus each tool's inputSchema,
# which is where a consolidated tool's `action` enum lives (manage_task →
# create/update/delete/move/duplicate). That enum is the thing worth reading
# before writing a prompt, so it's surfaced explicitly rather than left buried.
MCP_SCHEMA_CACHE = "/opt/data/cache/mcp_schema_cache.json"


def _tool_schema_facts(entry: dict) -> dict:
    """Pull the useful bits out of one cached tool schema."""
    schema = entry.get("inputSchema") or entry.get("input_schema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    props = props if isinstance(props, dict) else {}
    required = schema.get("required") if isinstance(schema, dict) else None
    required = [str(r) for r in required] if isinstance(required, list) else []

    # The action/operation enum a consolidated tool routes on. Servers name it
    # differently, so check the conventional keys in order of likelihood.
    actions: list[str] = []
    for key in ("action", "operation", "mode", "command"):
        spec = props.get(key)
        if isinstance(spec, dict) and isinstance(spec.get("enum"), list):
            actions = [str(v) for v in spec["enum"]]
            break

    return {
        "full_description": str(entry.get("description") or ""),
        "actions": actions,
        "required_params": [p for p in required if p in props] or required,
        "params": sorted(props.keys()),
    }


async def _mcp_schema_cache() -> dict:
    """{server: {tool: {full_description, actions, params, ...}}} or {}."""
    try:
        out = await _run_out(*_exec_prefix(), "cat", MCP_SCHEMA_CACHE, timeout=20)
        raw = json.loads(out[out.index("{"):])
    except Exception:  # noqa: BLE001
        return {}
    cache: dict = {}
    for server, block in (raw.items() if isinstance(raw, dict) else []):
        if not isinstance(block, dict):
            continue
        tools: dict = {}
        # `utility_tools` holds the resources/prompts helpers Hermes registers
        # alongside the server's own tools; both are real, callable tools.
        for group in ("tools", "utility_tools"):
            for entry in (block.get(group) or []):
                if isinstance(entry, dict) and entry.get("name"):
                    tools[str(entry["name"])] = _tool_schema_facts(entry)
        cache[server] = tools
    return cache


def _recycle_settings(cfg: dict) -> dict:
    """Whether Hermes' stdio auto-recycle / lazy-reconnect is armed for a server.

    Hermes only reconnects a dead stdio server on its own when it considers that
    server "recycled stdio" (_is_recycled_stdio), and that requires an idle or
    lifetime limit to be configured. Without one, a server that dies mid-run
    stays dead for the rest of that `hermes -z` process. Reported so the panel
    can say so rather than leaving it invisible.
    """
    lifecycle = cfg.get("lifecycle") if isinstance(cfg.get("lifecycle"), dict) else {}

    def _num(key: str):
        raw = cfg.get(key, lifecycle.get(key))
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        return val if val > 0 else None

    idle, lifetime, keepalive = _num("idle_timeout_seconds"), _num("max_lifetime_seconds"), _num("keepalive_interval")
    return {
        "idle_timeout_seconds": idle,
        "max_lifetime_seconds": lifetime,
        "keepalive_interval": keepalive,
        # HTTP servers reconnect per request; the recycle flag is stdio-only.
        "auto_reconnect": bool(idle or lifetime),
    }


def _mcp_env_refs(cfg: dict) -> list[str]:
    """Every ${VAR} referenced by a server's env values / HTTP headers."""
    refs: list[str] = []
    for block in (cfg.get("env"), cfg.get("headers")):
        if not isinstance(block, dict):
            continue
        for value in block.values():
            refs += MCP_ENV_REF_RE.findall(str(value or ""))
    return sorted(set(refs))


def _parse_mcp_test(output: str) -> dict:
    """Turn `hermes mcp test <name>` console output into a structured result."""
    res: dict = {"connected": False, "connect_ms": 0, "error": "",
                 "tools": [], "tool_count": 0}
    m = MCP_CONNECTED_RE.search(output)
    if m:
        res["connected"] = True
        res["connect_ms"] = int(m.group(1))
    m = MCP_FAILED_RE.search(output)
    if m:
        res["connect_ms"] = int(m.group(1))
        res["error"] = m.group(2).strip()
    m = MCP_TOOLCOUNT_RE.search(output)
    if m:
        res["tool_count"] = int(m.group(1))
    # Tool rows only appear after the "Tools discovered" banner.
    seen_banner = False
    for line in output.splitlines():
        if MCP_TOOLCOUNT_RE.search(line):
            seen_banner = True
            continue
        if not seen_banner:
            continue
        hit = MCP_TOOL_LINE_RE.match(line)
        if hit:
            res["tools"].append({"name": hit.group(1),
                                 "description": hit.group(2).strip()})
    if not res["tool_count"]:
        res["tool_count"] = len(res["tools"])
    return res


def _selected_tool_names(cfg: dict, discovered: list[dict]) -> list[str]:
    """Which discovered tools survive this server's tools.include/exclude filter
    — i.e. what the agent is actually offered, not just what the server has."""
    names = [t["name"] for t in discovered]
    tools_cfg = cfg.get("tools")
    if not isinstance(tools_cfg, dict):
        return names
    inc = tools_cfg.get("include")
    exc = tools_cfg.get("exclude")
    if isinstance(inc, list) and inc:
        allow = {str(x) for x in inc}
        names = [n for n in names if n in allow]
    if isinstance(exc, list) and exc:
        deny = {str(x) for x in exc}
        names = [n for n in names if n not in deny]
    return names


async def _probe_mcp_server(name: str, cfg: dict, env_names: set[str] | None,
                            schemas: dict | None = None) -> dict:
    """Config + live health for one MCP server.

    States, in the order they're decided:
      missing_binary — a stdio server whose command doesn't exist in the
                      container. Decided FIRST and without running the
                      connection test, because that test would spend the whole
                      connect_timeout to arrive at a bare "Connection closed".
      auth_required — a ${VAR} the server needs is unset, OR the connection
                      failed with an auth-shaped error. The server may well be
                      reachable and advertising tools; it just can't authenticate.
      unreachable   — the connection failed for any other reason.
      ok            — connected, and every referenced credential resolves.
    """
    transport = "http" if cfg.get("url") else "stdio"
    target = str(cfg.get("url") or cfg.get("command") or "?")
    if transport == "stdio" and cfg.get("args"):
        target = " ".join([target] + [str(a) for a in cfg["args"]])

    refs = _mcp_env_refs(cfg)
    # env_names is None when the probe failed — leave `missing` empty so health
    # falls back to what the connection test alone can prove, rather than
    # accusing a correctly-configured server of having no credentials.
    missing = [r for r in refs if r not in env_names] if env_names is not None else []

    # Bound the wait: a dead stdio server burns the whole connect_timeout before
    # `hermes mcp test` gives up, so allow for that plus process startup.
    try:
        connect_timeout = float(cfg.get("connect_timeout") or 60)
    except (TypeError, ValueError):
        connect_timeout = 60.0
    budget = max(30.0, min(connect_timeout + 25.0, 200.0))

    recycle = _recycle_settings(cfg)
    result = {"connected": False, "connect_ms": 0, "error": "", "tools": [], "tool_count": 0}
    stderr_tail = ""

    # Preflight: a stdio command that isn't installed can be diagnosed in ~1s,
    # and answers the question the connection test cannot (see
    # _stdio_command_exists). Skipped for HTTP servers, which have no command.
    binary_ok = await _stdio_command_exists(cfg) if transport == "stdio" else None

    if binary_ok is False:
        state = "missing_binary"
        result["error"] = f"command not found in the container: {cfg.get('command')}"
    else:
        async def _test() -> dict:
            try:
                _, out = await asyncio.wait_for(
                    _run(*_exec_prefix(), "hermes", "mcp", "test", name), timeout=budget)
                return _parse_mcp_test(out)
            except asyncio.TimeoutError:
                return {"connected": False, "connect_ms": 0, "tools": [], "tool_count": 0,
                        "error": f"health check exceeded {int(budget)}s"}
            except Exception as e:  # noqa: BLE001
                return {"connected": False, "connect_ms": 0, "tools": [], "tool_count": 0,
                        "error": str(e)}

        result = await _test()
        # One retry, only for transient-shaped failures. A real misconfiguration
        # fails identically twice, so this costs nothing in the common bad case
        # and rescues the panel from a spurious red on a startup race.
        if result.get("error") and any(h in result["error"].lower() for h in MCP_TRANSIENT_HINTS):
            retried = await _test()
            if retried.get("connected") or not retried.get("error"):
                result = retried
            else:
                result["error"] = retried["error"]
                result["retried"] = True

        err_l = (result["error"] or "").lower()
        auth_shaped = any(h in err_l for h in MCP_AUTH_HINTS)
        if missing or (result["error"] and auth_shaped):
            state = "auth_required"
        elif result["error"] or not result["connected"]:
            state = "unreachable"
        else:
            state = "ok"

    # Whatever went wrong, the server's own stderr is the most useful thing we
    # can show — and for a masked ENOENT it is the only record that exists.
    if state in ("missing_binary", "unreachable") and transport == "stdio":
        stderr_tail = await _mcp_stderr_tail(name)

    if state == "missing_binary":
        # Name the actual remediation. This failure is nearly always a stale
        # image: `docker compose up -d` reuses the existing one, so a server
        # added to the Dockerfile after the last --build is simply not there.
        detail = (
            f"The agent container has no `{cfg.get('command')}` on its PATH, so the "
            "server can never start — Hermes reports this only as a closed pipe. "
            "If it's installed in the image, rebuild the agent: "
            "`docker compose up -d --build` in DEV/hermes-docker (a plain "
            "`up -d` reuses the old image and changes nothing)."
        )
    elif missing:
        # /opt/data/.env is where Hermes loads dotenv from, but in this compose
        # setup that path is a read-only bind of hermes-docker/.env (the
        # ${PWD}/.env line) — editing ~/.hermes/.env there has no effect, so
        # name the file that actually wins.
        detail = ("Credentials not configured: "
                  + ", ".join(missing)
                  + f". Set {'them' if len(missing) > 1 else 'it'} in the agent's "
                    "/opt/data/.env (bind-mounted from DEV/hermes-docker/.env) "
                    "— no rebuild or restart, each turn re-reads it.")
        if result["connected"]:
            # The interesting case: Hermes leaves the placeholder literal, so
            # the process starts and lists its tools; the API rejects the bogus
            # token only when a tool is actually called.
            detail += (" The server is reachable and its tools are listed, but "
                       "every call will fail authentication until then.")
        elif result["error"]:
            detail += f" Connection also failed: {result['error']}"
    elif result["error"]:
        detail = result["error"]
    else:
        detail = f"Connected in {result['connect_ms']}ms · {result['tool_count']} tools discovered"

    # Enrich the probe's truncated tool rows with the cached full schema. The
    # console description stays as the collapsed preview; `full_description`,
    # `actions` and `params` are what the panel reveals on expand.
    cached = (schemas or {}).get(name) or {}
    for tool in result["tools"]:
        facts = cached.get(tool.get("name"))
        if not facts:
            continue
        tool.update(facts)
        # A description ending in "..." is the console's truncation, not the
        # server's text — drop the ellipsis once the real text is attached.
        if facts["full_description"] and tool.get("description", "").endswith("..."):
            tool["truncated"] = True

    selected = _selected_tool_names(cfg, result["tools"])
    return {
        "name": name,
        "transport": transport,
        "target": target,
        "state": state,
        "connected": result["connected"],
        "connect_ms": result["connect_ms"],
        "error": result["error"],
        "detail": detail,
        "tools": result["tools"],
        "tool_count": result["tool_count"],
        # Prefixed exactly as the agent sees them, so a tool name in the chat
        # transcript can be traced straight back to this panel.
        "selected_tools": selected,
        "selected_prefixed": [f"mcp__{name}__{t}" for t in selected],
        "env_refs": refs,
        "missing_env": missing,
        # Diagnostics the panel shows when something is wrong. stderr_tail is
        # the server's own output — for a masked ENOENT it holds the only
        # actual traceback anywhere in the system.
        "stderr_tail": stderr_tail,
        "binary_ok": binary_ok,
        "retried": bool(result.get("retried")),
        # Is Hermes' own auto-recycle armed for this server? A stdio server
        # without it never comes back on its own after dying mid-run.
        "recycle": recycle,
    }


MCP_CACHE: dict = {"ts": 0.0, "data": None}
_MCP_LOCK = asyncio.Lock()


@app.get("/api/mcp")
async def mcp_status(refresh: int = 0):
    """Configured MCP servers, their health, and the tools each one exposes.

    Probing spawns `hermes mcp test` per server (seconds each, longer when one
    is down), so results are cached for 60s and every server is probed
    concurrently. `?refresh=1` forces a re-probe. The lock keeps a burst of
    clients — the UI polls this — from stacking N identical probe runs.
    """
    import time

    now = time.time()
    if not refresh and MCP_CACHE["data"] and now - MCP_CACHE["ts"] < 60:
        return MCP_CACHE["data"]

    async with _MCP_LOCK:
        now = time.time()
        if not refresh and MCP_CACHE["data"] and now - MCP_CACHE["ts"] < 60:
            return MCP_CACHE["data"]

        servers_cfg: dict = {}
        config_error = ""
        try:
            _, out = await asyncio.wait_for(
                _run(*_exec_prefix(), "cat", "/opt/data/config.yaml"), timeout=15)
            cfg = yaml.safe_load(out) or {}
            raw = cfg.get("mcp_servers")
            if isinstance(raw, dict):
                servers_cfg = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except Exception as e:  # noqa: BLE001
            config_error = str(e)

        env_names = await _container_env_names() if servers_cfg else None
        schemas = await _mcp_schema_cache() if servers_cfg else {}
        results = await asyncio.gather(
            *(_probe_mcp_server(n, c, env_names, schemas) for n, c in servers_cfg.items()),
            return_exceptions=True,
        )
        servers = []
        for (name, _cfg), r in zip(servers_cfg.items(), results):
            if isinstance(r, BaseException):
                servers.append({"name": name, "state": "unreachable", "connected": False,
                                "transport": "?", "target": "?", "detail": str(r),
                                "error": str(r), "tools": [], "tool_count": 0,
                                "selected_tools": [], "selected_prefixed": [],
                                "env_refs": [], "missing_env": [], "connect_ms": 0})
            else:
                servers.append(r)
        servers.sort(key=lambda s: s["name"])

        data = {
            "servers": servers,
            "config_error": config_error,
            "checked_at": time.time(),
            "summary": {
                "total": len(servers),
                "ok": sum(1 for s in servers if s["state"] == "ok"),
                "auth_required": sum(1 for s in servers if s["state"] == "auth_required"),
                "unreachable": sum(1 for s in servers if s["state"] == "unreachable"),
                "missing_binary": sum(1 for s in servers if s["state"] == "missing_binary"),
                "tools": sum(len(s.get("selected_prefixed") or []) for s in servers),
            },
        }
        MCP_CACHE.update(ts=time.time(), data=data)
        return data


# ── Skills ───────────────────────────────────────────────────────────────
# Hermes builds a skills INDEX into every system prompt — name + one-line
# description per skill — and only reads a SKILL.md body when the model
# actually invokes that skill. This endpoint mirrors that boundary exactly: it
# lists what the agent currently has available and never opens a SKILL.md, so
# checking "what can it do?" costs nothing and can't move the context needle.
#
# `hermes skills list` prints a Rich table with no --json mode. Rich sizes to
# the terminal, and a non-TTY `docker exec` defaults to 80 columns, which
# truncates longer names with an ellipsis ("songwriting-and-ai-mus…") — useless
# for a name list. COLUMNS=200 makes Rich lay the table out wide enough that
# nothing is elided.
SKILLS_CACHE: dict = {"ts": 0.0, "data": None}


def _parse_skills_table(output: str) -> list[dict]:
    """Rows of `hermes skills list` → [{name, category, source, trust, status}]."""
    skills: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        # Data rows are the ones delimited by box-drawing pipes; the ┏━┳┓ rules
        # and the title line are not.
        if not line.startswith("│"):
            continue
        cells = [c.strip() for c in line.strip("│").split("│")]
        if len(cells) < 5:
            continue
        name = cells[0]
        if not name or name.lower() == "name":   # header row
            continue
        skills.append({
            "name": name,
            "category": cells[1] or "uncategorised",
            "source": cells[2],
            "trust": cells[3],
            "status": cells[4],
        })
    return skills


@app.get("/api/skills")
async def skills_info(refresh: int = 0):
    """Skills available to the agent right now — names only, no SKILL.md reads.

    `--enabled-only` is what makes this answer "what will actually load for the
    next turn" rather than "what happens to be installed on disk". Cached for
    5 minutes; the set only changes when a skill is installed or toggled.
    """
    import time

    now = time.time()
    if not refresh and SKILLS_CACHE["data"] and now - SKILLS_CACHE["ts"] < 300:
        return SKILLS_CACHE["data"]

    skills: list[dict] = []
    error = ""
    try:
        _, out = await asyncio.wait_for(
            _run(*_exec_prefix({"COLUMNS": "200"}),
                 "hermes", "skills", "list", "--enabled-only"), timeout=60)
        skills = _parse_skills_table(out)
        if not skills:
            error = "no skills parsed from `hermes skills list`"
    except Exception as e:  # noqa: BLE001
        error = str(e)

    categories: dict = {}
    sources: dict = {}
    for s in skills:
        categories[s["category"]] = categories.get(s["category"], 0) + 1
        sources[s["source"]] = sources.get(s["source"], 0) + 1

    data = {
        "skills": sorted(skills, key=lambda s: (s["category"], s["name"])),
        "total": len(skills),
        "categories": dict(sorted(categories.items())),
        "sources": dict(sorted(sources.items())),
        "error": error,
        "checked_at": time.time(),
    }
    SKILLS_CACHE.update(ts=time.time(), data=data)
    return data


async def _turn_alive_in_container(session: str) -> bool:
    """Is the hermes process for this turn still running inside the container?
    Covers the case where the webui restarted mid-turn and lost its handle."""
    try:
        p = await asyncio.create_subprocess_exec(
            *_exec_prefix(), "pgrep", "-f", "--", f"--resume {session}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return (await asyncio.wait_for(p.wait(), timeout=15)) == 0
    except Exception:  # noqa: BLE001
        return False


def _status_label(events: list[dict]) -> str:
    """A human label for what the turn is doing right now, from its last event."""
    for e in reversed(events or []):
        k = e.get("kind")
        if k == "call":
            return f"Running tool: {e.get('name', '?')}"
        if k == "result":
            return "Processing tool result"
        if k == "interim":
            return "Working…"
        if k == "model_switch":
            return f"Switched to {e.get('to', '?')} (rate limited) — continuing…"
        if k == "chunk":
            return "Generating response…"
    return "Processing prompt…"


@app.get("/api/turn/{session}")
async def turn(session: str):
    """Reattach point for a turn whose stream was lost.

    Answers "ну що там?" with the server-side truth, checking in order:
      1. the turn record (memory, then disk — survives a webui restart);
      2. whether the process is still alive (locally or in the container);
      3. the Hermes session store itself, via the recorded turn_id — a reply
         that finished while nobody was attached is recovered from there.
    Only when every source comes up empty does it report failed=true, which
    the client renders as "Prompt processing failed."
    """
    if not TURN_KEY_RE.fullmatch(session):
        return JSONResponse({"error": "bad turn key"}, status_code=400)

    rec = TURNS.get(session) or _load_turn(session)
    if rec and rec.get("status") == "done":
        return {"done": True, "running": False, "failed": False,
                "status": "done", "text": rec.get("text", ""),
                "events": rec.get("events", []), "code": rec.get("code")}

    alive = session in RUNNING or await _turn_alive_in_container(session)
    if alive:
        # Live view for a reattaching client: discover the turn's forked
        # session if needed and export it fresh, so completed sub-steps show
        # up without waiting for the background poller's next tick.
        events: list[dict] = []
        if rec is not None:
            TURNS.setdefault(session, rec)
            if not rec.get("turn_id"):
                nid = await _latest_cli_session_id()
                if nid and nid != rec.get("pre_id", ""):
                    rec["turn_id"] = nid
                    _persist_turn(session, rec)
            if rec.get("turn_id"):
                try:
                    events, _, _ = await _export_turn(rec["turn_id"])
                except Exception:  # noqa: BLE001
                    events = []
            if not events:
                events = [e for e in rec.get("events", []) if e.get("kind") != "chunk"]
        label = _status_label(rec.get("events", []) if rec else events)
        return {"done": False, "running": True, "failed": False,
                "status": "running", "label": label,
                "started": (rec or {}).get("ts0") or (rec or {}).get("ts"),
                "text": "", "events": events}

    # Process gone with no finished record — last resort: ask the Hermes
    # store whether this turn's session holds a completed reply.
    if rec and rec.get("turn_id"):
        events, final_text, _ = await _export_turn(rec["turn_id"])
        if final_text or events:
            rec.update(status="done", text=final_text or rec.get("text", ""),
                       events=events, code=rec.get("code") or 0)
            TURNS[session] = rec
            _persist_turn(session, rec)
            return {"done": True, "running": False, "failed": False,
                    "status": "done", "text": rec["text"],
                    "events": events, "code": rec["code"]}

    return {"done": False, "running": False, "failed": True,
            "status": "failed", "text": "", "events": []}


@app.post("/api/turn/{session}/ack")
async def turn_ack(session: str):
    """Client confirms it received the turn's outcome; the record is dropped.
    Until acked, the record is kept so reconnects can replay it."""
    if not TURN_KEY_RE.fullmatch(session):
        return JSONResponse({"error": "bad turn key"}, status_code=400)
    _drop_turn(session)
    return {"ok": True}


@app.post("/api/chat")
async def chat(body: ChatBody):
    msg = body.message.strip()
    if not msg:
        return JSONResponse({"error": "empty message"}, status_code=400)
    session = body.session.strip() or "webui_default"
    return StreamingResponse(
        _stream_chat(body.history or [], msg, session, body.agent_mode,
                     body.model.strip(), body.provider.strip()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class StopBody(BaseModel):
    session: str


@app.post("/api/stop")
async def stop(body: StopBody):
    """Stop the in-flight response for a session (kills the running hermes turn)."""
    session = body.session.strip()
    if not session:
        return JSONResponse({"error": "no session"}, status_code=400)
    proc = RUNNING.get(session)
    if proc is not None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    # Always pkill inside the container too — the exec'd process can outlive
    # its local client, and there may be no local handle after a reconnect.
    await _kill_container_chat(session)
    return {"ok": True, "had_local_process": proc is not None}


if __name__ == "__main__":
    import uvicorn

    kwargs: dict = {}
    # WEBUI_TLS=1 serves HTTPS with the self-signed cert the entrypoint
    # generates, so the auth token and conversation content aren't sniffable
    # on the LAN. Browsers warn once about the self-signed cert; accept it.
    if os.environ.get("WEBUI_TLS", "") == "1":
        cert = Path("/app/certs/server.crt")
        key = Path("/app/certs/server.key")
        if cert.exists() and key.exists():
            kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        else:
            print("WEBUI_TLS=1 but no cert found — serving plain HTTP", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), **kwargs)
