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

# Markers wrap the preamble in the sent prompt so we can strip it back out when
# rendering a stored transcript — the user only ever sees their own text.
PREAMBLE_OPEN = "<<webui-context>>"
PREAMBLE_CLOSE = "<</webui-context>>"
PREAMBLE_BLOCK_RE = re.compile(
    re.escape(PREAMBLE_OPEN) + r".*?" + re.escape(PREAMBLE_CLOSE) + r"\s*",
    re.DOTALL,
)

app = FastAPI(title="Hermes WebUI")

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


def _compose_prompt(history: list[dict], message: str) -> str:
    """Build a single prompt carrying the whole conversation.

    `hermes -z` is one-shot: each call forks a fresh session and does NOT
    reliably carry prior turns forward. So the webui owns the conversation and
    injects the full history into every prompt — that gives the model correct,
    explicit context regardless of Hermes' session store. The system preamble
    (if any) rides at the top.
    """
    parts: list[str] = []
    if SYSTEM_PREAMBLE:
        parts.append(SYSTEM_PREAMBLE)
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
# Completed assistant replies, keyed by per-turn session key, so a reply that
# finished while the client was away (phone locked) can be recovered on return.
DONE_BUFFERS: dict[str, str] = {}
_DONE_BUFFERS_MAX = 40


def _buffer_reply(session: str, text: str) -> None:
    DONE_BUFFERS[session] = text
    while len(DONE_BUFFERS) > _DONE_BUFFERS_MAX:
        DONE_BUFFERS.pop(next(iter(DONE_BUFFERS)))


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


async def _stream_chat(history: list[dict], message: str, session: str):
    """Run the Hermes one-shot CLI and yield SSE events as output arrives.

    `session` is a unique per-turn key: it isolates this turn's Hermes session
    (a fresh name starts clean) and is the handle /api/stop uses to kill it.
    Context comes from the injected history, not from Hermes' session store.
    """
    args = _exec_prefix() + [
        "hermes", "-z", _compose_prompt(history, message),
        "--resume", session,
        "--yolo", "--cli",
    ]
    if DEFAULT_MODEL:
        args += ["-m", DEFAULT_MODEL]

    def sse(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

    yield sse("start", {"session": session})

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

    RUNNING[session] = proc
    detached = False
    captured: list[str] = []
    assert proc.stdout is not None
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = ANSI_RE.sub("", raw.decode(errors="replace")).rstrip("\n")
            captured.append(line)
            yield sse("chunk", {"text": line})
    except asyncio.CancelledError:
        # The client dropped — e.g. the phone locked and the browser suspended
        # the tab. Do NOT kill the turn: let it finish server-side and buffer
        # the reply so the client can recover it on return. Deliberate stops go
        # through /api/stop, which kills on demand.
        detached = True
        asyncio.create_task(_finish_detached(proc, session, captured))
        raise
    finally:
        if not detached:
            if RUNNING.get(session) is proc:
                del RUNNING[session]
            try:
                rc = await proc.wait()
            except Exception:  # noqa: BLE001
                rc = -1
            _buffer_reply(session, "\n".join(captured))
            yield sse("done", {"code": rc, "stopped": False})


async def _finish_detached(proc, session: str, captured: list[str]) -> None:
    """Drain a chat process to completion after the client left, then reap it.

    Keeps reading stdout so the pipe never fills (which would block hermes
    mid-turn) and buffers the full reply for recovery via /api/turn.
    """
    try:
        if proc.stdout is not None:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                captured.append(ANSI_RE.sub("", raw.decode(errors="replace")).rstrip("\n"))
        await proc.wait()
    except Exception:  # noqa: BLE001
        pass
    finally:
        if RUNNING.get(session) is proc:
            del RUNNING[session]
        _buffer_reply(session, "\n".join(captured))


@app.get("/api/turn/{session}")
async def turn(session: str):
    """Recover a reply that finished while the client was away (phone locked).

    `done` is true once the turn's process is no longer running; `text` is the
    full raw output captured for that per-turn session key.
    """
    running = session in RUNNING
    text = DONE_BUFFERS.get(session, "")
    return {"done": (not running) and (session in DONE_BUFFERS), "running": running, "text": text}


@app.post("/api/chat")
async def chat(body: ChatBody):
    msg = body.message.strip()
    if not msg:
        return JSONResponse({"error": "empty message"}, status_code=400)
    session = body.session.strip() or "webui_default"
    return StreamingResponse(
        _stream_chat(body.history or [], msg, session),
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

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
