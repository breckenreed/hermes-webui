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
    session: str  # stable resume-key for this conversation


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
        # Columns are separated by runs of 2+ spaces: Title | Workspace | LastActive
        cols = [c.strip() for c in re.split(r"\s{2,}", left) if c.strip()]
        title = cols[0] if cols else ""
        if title in ("—", "-", ""):
            title = ""
        last_active = cols[-1] if len(cols) >= 2 else ""
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


async def _stream_chat(message: str, session: str):
    """Run the Hermes one-shot CLI and yield SSE events as output arrives."""
    args = _exec_prefix() + [
        "hermes", "-z", message,
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

    assert proc.stdout is not None
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = ANSI_RE.sub("", raw.decode(errors="replace")).rstrip("\n")
            yield sse("chunk", {"text": line})
    except asyncio.CancelledError:  # client disconnected
        proc.kill()
        raise
    finally:
        rc = await proc.wait()
        yield sse("done", {"code": rc})


@app.post("/api/chat")
async def chat(body: ChatBody):
    msg = body.message.strip()
    if not msg:
        return JSONResponse({"error": "empty message"}, status_code=400)
    session = body.session.strip() or "webui_default"
    return StreamingResponse(
        _stream_chat(msg, session),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
