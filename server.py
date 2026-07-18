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
import shutil
from pathlib import Path

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
    "vault is bind-mounted read-write at /host/opser-local. Always use your "
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


def _exec_prefix() -> list[str]:
    """docker exec into the Hermes container, passing the LLM key through."""
    cmd = _docker("exec", "-i")
    if LLM_CLIENT_UID:
        cmd += ["-e", f"LLM_CLIENT_UID={LLM_CLIENT_UID}"]
    cmd += [HERMES_CONTAINER]
    return cmd


class ChatBody(BaseModel):
    message: str
    session: str                      # unique per-turn key (isolation + stop handle)
    history: list[dict] = []          # prior [{role, text}] turns, injected as context
    agent_mode: bool = False          # add completion pressure for multi-step / todo work


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


async def _export_turn(sid: str) -> tuple[list[dict], str]:
    """Ordered activity of one Hermes session + its final reply text.

    Events mirror the agent's actions into the chat, Claude-code style:
      call    — a tool invocation (name + args)
      result  — what the tool returned
      interim — an intermediate assistant message (a finished sub-step),
                emitted only for messages that are NOT the last one, so the
                final reply (which arrives via stdout) is never duplicated.
    Read-only, so polling it mid-turn is safe.
    """
    try:
        out = await _run_out(
            *_exec_prefix(), "hermes", "sessions", "export",
            "--format", "jsonl", "--session-id", sid, "-", timeout=25)
        out = out.strip()
        obj = json.loads(out[out.index("{"):].splitlines()[0])
    except Exception:  # noqa: BLE001
        return [], ""
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
    return events, final_text


async def _stream_chat(history: list[dict], message: str, session: str,
                       agent_mode: bool = False):
    """Run the Hermes one-shot CLI and yield SSE events as output arrives.

    `session` is a unique per-turn key: it isolates this turn's Hermes session
    (a fresh name starts clean) and is the handle /api/stop uses to kill it.
    Context comes from the injected history, not from Hermes' session store.

    While the turn runs, a poller watches the forked Hermes session and emits
    `tool` events (calls + results) so the UI can show the agent's actions
    live, Claude-code style.
    """
    args = _exec_prefix() + [
        "hermes", "-z", _compose_prompt(history, message, agent_mode),
        "--resume", session,
        "--yolo", "--cli",
    ]
    if DEFAULT_MODEL:
        args += ["-m", DEFAULT_MODEL]

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
                events, final_text = await _export_turn(rec["turn_id"])
                known = sum(1 for e in rec["events"] if e.get("kind") != "chunk")
                for ev in events[known:]:
                    ev.setdefault("ts", time.time())
                    rec["events"].append(ev)
                if not rec["text"] and final_text:
                    rec["text"] = final_text
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
                events, _ = await _export_turn(rec["turn_id"])
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
        streamed_tools = 0
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
            if kind == "tool":
                streamed_tools += 1
            yield sse(kind, data)
        poller.cancel()
        # Replay tool/interim events finish_record added after the live
        # stream ended (turns faster than the poll interval).
        non_chunk = [e for e in rec["events"] if e.get("kind") != "chunk"]
        for ev in non_chunk[streamed_tools:]:
            yield sse("tool", ev)
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
                    events, _ = await _export_turn(rec["turn_id"])
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
        events, final_text = await _export_turn(rec["turn_id"])
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
        _stream_chat(body.history or [], msg, session, body.agent_mode),
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
