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
import secrets
import shlex
import shutil
import tempfile
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
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
#
# It deliberately names NO specific path. An earlier version hardcoded one
# installation's vault location, which every deployment then inherited: the
# model was told a directory existed, found nothing there, and spent the turn
# reasoning about the missing path instead of looking. A wrong path is worse
# than no path — it overrides what the tools would have shown. Anything
# site-specific belongs in HERMES_SYSTEM_PREAMBLE, per install.
_DEFAULT_PREAMBLE = (
    "You are running inside a Linux container (not WSL). Always use your "
    "tools to inspect the filesystem and find where things actually live — "
    "never guess about your environment or the location of files."
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

# A token short enough to guess is not a token. 16 chars of the
# `openssl rand -hex 16` the README suggests is the floor we warn below.
TOKEN_MIN_LENGTH = 16

# Brute-force throttle. The token is a single shared secret with no account
# lockout behind it, so an unthrottled endpoint is a pure online guessing
# oracle — at LAN speed a short token falls in minutes. After
# AUTH_MAX_FAILURES bad tokens an IP is refused outright for
# AUTH_LOCKOUT_SECONDS, which turns that into a rate no longer worth running.
AUTH_MAX_FAILURES = int(os.environ.get("WEBUI_AUTH_MAX_FAILURES", "10"))
AUTH_LOCKOUT_SECONDS = float(os.environ.get("WEBUI_AUTH_LOCKOUT_SECONDS", "60"))
# Bound the table so a spoofed-source flood can't grow it without limit.
_AUTH_TABLE_MAX = 1024
# ip -> {"fails": int, "until": float, "last": str}
_AUTH_FAILURES: dict[str, dict] = {}


def _token_digest(value: str) -> str:
    """Short fingerprint of a rejected token, for repeat detection.

    Hashed rather than stored: the counter only needs to know whether this is
    the same wrong value again, and keeping user-supplied secrets in memory
    (a mistyped *correct* token, say) is not worth it.
    """
    import hashlib

    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def _client_ip(request: Request) -> str:
    """Source address for throttling.

    Deliberately NOT X-Forwarded-For: that header is attacker-controlled on a
    directly-exposed port, so honouring it would let a single client mint a
    fresh identity per guess and walk straight through the lockout.
    """
    return (request.client.host if request.client else "") or "unknown"


def _auth_locked(ip: str) -> float:
    """Seconds remaining on this IP's lockout, or 0 when it may try."""
    entry = _AUTH_FAILURES.get(ip)
    if not entry:
        return 0.0
    if not entry["until"]:
        # Counting failures but not locked yet. Must NOT be discarded here:
        # dropping the entry on every unlocked request resets the tally each
        # time and the threshold is then never reached.
        return 0.0
    remaining = entry["until"] - time.time()
    if remaining <= 0:
        # A real lockout that has now elapsed — clear it so a client that
        # waited it out starts clean rather than one failure from the next.
        _AUTH_FAILURES.pop(ip, None)
        return 0.0
    return remaining


def _auth_record_failure(ip: str, supplied: str = "") -> None:
    """Count a rejected token — once per DISTINCT wrong value.

    Counting requests instead would lock users out of their own webui: the page
    fires ~6 parallel /api/ calls on load, so a single stale token in
    localStorage (after the operator rotates WEBUI_TOKEN) burns 6 of the
    allowance per reload, and the second reload locks the tab out — including
    the unlock request itself, which is what makes it unrecoverable.

    A brute-force run never repeats a guess, so it is counted exactly as
    before; only an immediate repeat of the *same* wrong value is free.
    """
    if len(_AUTH_FAILURES) >= _AUTH_TABLE_MAX:
        # Evict whatever expires soonest — it is the least informative entry.
        oldest = min(_AUTH_FAILURES, key=lambda k: _AUTH_FAILURES[k]["until"])
        _AUTH_FAILURES.pop(oldest, None)
    entry = _AUTH_FAILURES.setdefault(ip, {"fails": 0, "until": 0.0, "last": ""})
    digest = _token_digest(supplied)
    if entry.get("last") == digest:
        return                      # same wrong token again — already counted
    entry["last"] = digest
    entry["fails"] += 1
    if entry["fails"] >= AUTH_MAX_FAILURES:
        entry["fails"] = 0
        entry["until"] = time.time() + AUTH_LOCKOUT_SECONDS


def _token_ok(supplied: str) -> bool:
    """Constant-time token comparison that cannot raise.

    hmac.compare_digest() rejects non-ASCII `str` inputs with TypeError, so a
    header carrying e.g. Cyrillic used to surface as a 500 with a traceback
    instead of a clean 401. Comparing UTF-8 bytes keeps the timing property
    and accepts any input.
    """
    try:
        return hmac.compare_digest(supplied.encode("utf-8"),
                                   WEBUI_TOKEN.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


def _audit_token_config() -> list[str]:
    """Warnings about the current access-control setup, worst first.

    Returned rather than printed so a test can assert on them; the server
    prints them once at startup.
    """
    warnings: list[str] = []
    if not WEBUI_TOKEN:
        warnings.append(
            "WEBUI_TOKEN is not set — anyone who can reach this port gets full "
            "agent access, including unconfirmed file writes (--yolo). Safe "
            "only when the port is bound to localhost.")
    elif len(WEBUI_TOKEN) < TOKEN_MIN_LENGTH:
        warnings.append(
            f"WEBUI_TOKEN is only {len(WEBUI_TOKEN)} characters — use at least "
            f"{TOKEN_MIN_LENGTH} (`openssl rand -hex 16`).")
    if WEBUI_TOKEN and os.environ.get("WEBUI_TLS", "") != "1":
        warnings.append(
            "WEBUI_TLS is off — the access token crosses the network in "
            "cleartext. Set WEBUI_TLS=1 on anything but localhost.")
    return warnings


@app.middleware("http")
async def require_token(request: Request, call_next):
    if WEBUI_TOKEN and request.url.path.startswith("/api/"):
        ip = _client_ip(request)
        locked = _auth_locked(ip)
        if locked > 0:
            return JSONResponse(
                {"error": "too many failed attempts"},
                status_code=429,
                headers={"Retry-After": str(int(locked) + 1)},
            )
        supplied = request.headers.get("authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[7:]
        if not _token_ok(supplied):
            _auth_record_failure(ip, supplied)
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        _AUTH_FAILURES.pop(ip, None)   # a good token clears the streak
    return await call_next(request)


# ── Security headers ─────────────────────────────────────────────────────
# The chat pane renders model output as HTML (renderMarkdown -> innerHTML),
# and that output routinely quotes web pages and files the agent just read —
# i.e. content an attacker may control. The escaping in index.html is the
# first line of defence; this CSP is the second, so that a bug in one is not
# game over. `script-src` carries a per-response nonce and NO 'unsafe-inline',
# which is what makes it worth having: an injected <script> or onerror=
# handler has no way to name a valid nonce and simply does not execute.
#
# 'unsafe-inline' remains for style-src only — index.html uses inline
# style="..." attributes in a handful of places, and a style injection cannot
# execute script under this policy.
STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "geolocation=(), microphone=(), camera=(), usb=(), payment=(), "
        "magnetometer=(), gyroscope=()"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _csp(nonce: str) -> str:
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        # Same-origin XHR/SSE only: nothing in this app should ever be able to
        # phone home, so an injected exfiltration beacon has nowhere to send.
        "connect-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'none'",
    ])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Attach the security headers to every response.

    Registered after require_token, which makes it the OUTER middleware —
    Starlette runs the most recently added first — so the headers land on
    401/429 rejections too, not only on responses that got past auth.
    """
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    response = await call_next(request)
    for key, value in STATIC_HEADERS.items():
        response.headers.setdefault(key, value)
    response.headers.setdefault("Content-Security-Policy", _csp(nonce))
    if os.environ.get("WEBUI_TLS", "") == "1":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith("/api/"):
        # Transcripts and the token-bearing requests that fetch them have no
        # business in a shared cache or a browser's disk cache.
        response.headers.setdefault("Cache-Control", "no-store")
    return response

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


# index.html is a single 100KB file served on every page load, so it is read
# once and re-read only when it changes on disk — an editor save is picked up
# without a restart, but a refresh loop does not re-read it 60 times.
_INDEX_CACHE: dict = {"mtime": -1.0, "html": ""}
# Both <script> blocks in index.html are bare opening tags; each needs the
# nonce or the strict script-src drops it and the page is inert.
_SCRIPT_OPEN_RE = re.compile(r"<script(?=[\s>])(?![^>]*\bnonce=)")


def _index_html() -> str:
    path = STATIC_DIR / "index.html"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _INDEX_CACHE["html"]
    if _INDEX_CACHE["mtime"] != mtime:
        _INDEX_CACHE.update(mtime=mtime, html=path.read_text(encoding="utf-8"))
    return _INDEX_CACHE["html"]


@app.get("/")
async def index(request: Request):
    nonce = getattr(request.state, "csp_nonce", "")
    html = _SCRIPT_OPEN_RE.sub(f'<script nonce="{nonce}"', _index_html())
    return HTMLResponse(html)


# ── Health ───────────────────────────────────────────────────────────────
# "Is the container running?" is NOT the same question as "can the agent take
# a turn?", and treating them as one is how the UI came to show a green light
# through an outage: the agent container exited cleanly and was revived by its
# restart policy, every in-flight turn died with it, and `{{.State.Running}}`
# said "true" the whole way through.
#
# So the probe actually runs the CLI. That costs a process spawn (~1-2s), which
# is too much for the UI's 15s poll, so the result is cached — the container
# check is cheap and stays live, and the expensive proof of life refreshes on
# its own schedule.
AGENT_PROBE_CACHE: dict = {"ts": 0.0, "ready": False, "detail": "", "started_at": ""}
AGENT_PROBE_TTL = 30.0
AGENT_PROBE_TIMEOUT = 25.0


async def _probe_agent_cli(started_at: str) -> tuple[bool, str]:
    """Can the Hermes CLI in the container actually answer? Cached per TTL.

    `hermes --version` is the cheapest call that proves the whole path works:
    docker exec reaches the container, the interpreter starts, and the package
    imports. A restart invalidates the cache immediately — the point of the
    probe is to notice exactly that, so it must never answer from state that
    predates it.
    """
    import time

    now = time.time()
    fresh = (now - AGENT_PROBE_CACHE["ts"] < AGENT_PROBE_TTL
             and AGENT_PROBE_CACHE["started_at"] == started_at)
    if fresh:
        return AGENT_PROBE_CACHE["ready"], AGENT_PROBE_CACHE["detail"]

    ready, detail = False, ""
    try:
        code, out = await asyncio.wait_for(
            _run(*_exec_prefix(), "hermes", "--version"), timeout=AGENT_PROBE_TIMEOUT)
        first = (out or "").strip().splitlines()[0] if out.strip() else ""
        ready = code == 0 and "hermes" in first.lower()
        detail = first[:120] if ready else (out or "")[:200].replace("\n", " ")
    except asyncio.TimeoutError:
        detail = f"CLI did not answer within {int(AGENT_PROBE_TIMEOUT)}s"
    except Exception as e:  # noqa: BLE001
        detail = str(e)[:200]

    AGENT_PROBE_CACHE.update(ts=now, ready=ready, detail=detail, started_at=started_at)
    return ready, detail


@app.get("/api/health")
async def health():
    """Whether the agent can actually take a turn — not merely whether its
    container is running.

    States the UI distinguishes:
      ok        — container up AND the CLI answered.
      degraded  — container up, CLI silent or erroring. Turns will fail; the
                  old health check called this green.
      offline   — container not running (or docker unreachable).

    `started_at` is what lets the client notice a restart: it changes when the
    container is recreated or revived by its restart policy, and any turn that
    began before that timestamp cannot have survived.
    """
    running, started_at, restarts, exit_code, oom = "", "", 0, None, False
    inspect_error = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *_docker("inspect", "-f",
                     "{{.State.Running}}|{{.State.StartedAt}}|{{.RestartCount}}"
                     "|{{.State.ExitCode}}|{{.State.OOMKilled}}",
                     HERMES_CONTAINER),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        text = (out or err).decode(errors="replace").strip()
        parts = text.split("|")
        if len(parts) == 5:
            running, started_at, restarts_s, exit_s, oom_s = parts
            restarts = int(restarts_s) if restarts_s.isdigit() else 0
            exit_code = int(exit_s) if exit_s.lstrip("-").isdigit() else None
            oom = oom_s.strip().lower() == "true"
        else:
            inspect_error = text[:200]
    except Exception as e:  # noqa: BLE001
        inspect_error = str(e)[:200]

    container_up = running.strip().lower() == "true"
    agent_ready, agent_detail = (False, inspect_error or "container is not running")
    if container_up:
        agent_ready, agent_detail = await _probe_agent_cli(started_at)

    state = "ok" if (container_up and agent_ready) else ("degraded" if container_up else "offline")
    return {
        # `ok` now means "usable", which is the whole point of this endpoint.
        "ok": state == "ok",
        "state": state,
        "container": HERMES_CONTAINER,
        # Kept as the raw inspect string for backwards compatibility with any
        # client reading it; `state` is what to branch on.
        "running": running or inspect_error,
        "agent_ready": agent_ready,
        "agent_detail": agent_detail,
        "started_at": started_at,
        "restart_count": restarts,
        "last_exit_code": exit_code,
        "oom_killed": oom,
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

# A turn key becomes a filename and is pasted into a `pkill -f` pattern, so
# the character set is constrained and every entry point checks it. Without
# that check a client-chosen session like "../../x" writes outside TURNS_DIR
# and one containing regex metacharacters changes what /api/stop kills.
#
# The first character is narrower than the rest on purpose: it rules out "..",
# dotfiles, and a leading "-" that a CLI could read as a flag. Keys the webui
# generates look like "c_<base36>__<base36>", which fits comfortably.
TURN_KEY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}")


def _valid_turn_key(key: str) -> bool:
    return bool(TURN_KEY_RE.fullmatch(key or ""))


def _resolve_turns_dir() -> Path:
    """Pick a writable directory for turn records, preferring a durable one.

    The previous default lived under /tmp *inside the container*, so every
    `docker compose up -d --build` threw away the records whose whole purpose
    is outliving a restart. /app/state is a named volume in the shipped
    compose file. The temp dir stays last in the list so a missing mount
    degrades to the old behaviour rather than failing to start.
    """
    candidates: list[Path] = []
    explicit = os.environ.get("TURNS_DIR", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path("/app/state/turns"))
    candidates.append(Path(tempfile.gettempdir()) / "hermes-webui-turns")
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return path
        except Exception:  # noqa: BLE001
            continue
    return candidates[-1]


TURNS_DIR = _resolve_turns_dir()

# Disk retention. The in-memory table is trimmed for footprint; disk keeps
# more, since surviving a restart is the point — but not without limit. Before
# this, records accumulated forever and nothing ever removed one.
TURNS_MAX_FILES = int(os.environ.get("TURNS_MAX_FILES", "200"))
TURNS_MAX_AGE_DAYS = float(os.environ.get("TURNS_MAX_AGE_DAYS", "7"))


def _turn_path(key: str) -> Path | None:
    return TURNS_DIR / f"{key}.json" if _valid_turn_key(key) else None


def _persist_turn(key: str, rec: dict) -> None:
    """Write a turn record atomically.

    A plain write_text() leaves a truncated file behind if the process dies
    mid-write, and at read time a truncated record is indistinguishable from a
    lost one. Writing a sibling and renaming means a reader sees either the
    previous record or the new one — never half of either.
    """
    path = _turn_path(key)
    if path is None:
        return
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _load_turn(key: str) -> dict | None:
    path = _turn_path(key)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        # Corrupt or unreadable — e.g. a truncation from before atomic writes.
        # Drop it so it cannot shadow the recovery path on every later retry.
        try:
            path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return None


def _drop_turn(key: str) -> None:
    TURNS.pop(key, None)
    path = _turn_path(key)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _trim_turns() -> None:
    """Bound the in-memory table only. Disk is the durable copy and has its
    own retention — see _prune_turn_files."""
    while len(TURNS) > _TURNS_MAX:
        TURNS.pop(next(iter(TURNS)))


def _prune_turn_files(now: float | None = None) -> int:
    """Delete records past the age or count limit. Returns how many went."""
    now = time.time() if now is None else now
    cutoff = now - TURNS_MAX_AGE_DAYS * 86400 if TURNS_MAX_AGE_DAYS > 0 else None
    entries: list[tuple[float, Path]] = []
    try:
        for path in TURNS_DIR.glob("*.json"):
            try:
                entries.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        return 0
    entries.sort(key=lambda e: e[0], reverse=True)     # newest first

    removed = 0
    for index, (mtime, path) in enumerate(entries):
        too_many = TURNS_MAX_FILES > 0 and index >= TURNS_MAX_FILES
        too_old = cutoff is not None and mtime < cutoff
        if not (too_many or too_old):
            continue
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass

    # Sweep stale .tmp siblings an interrupted write may have left. The age
    # guard keeps this from racing a write that is legitimately in progress.
    try:
        for path in TURNS_DIR.glob("*.json.tmp"):
            try:
                if now - path.stat().st_mtime > 300:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
    return removed


def _restore_turns() -> int:
    """Reload persisted records into memory at startup.

    Without this, a webui restart left TURNS empty, so a client reattaching to
    a turn that was in flight hit the on-disk path with no turn_id in hand and
    got told the turn failed — while the record sat on disk and, often, the
    hermes process was still running in the agent container. Newest first,
    capped at the in-memory limit.
    """
    try:
        paths = sorted(TURNS_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:  # noqa: BLE001
        return 0
    restored = 0
    for path in paths[:_TURNS_MAX]:
        key = path.name[: -len(".json")]
        rec = _load_turn(key)
        if rec is None:
            continue
        # Records still marked "running" are left as-is: /api/turn re-checks
        # liveness in the container, which is the only authority on whether
        # the turn outlived this process.
        TURNS.setdefault(key, rec)
        restored += 1
    return restored


async def _kill_container_chat(session: str) -> None:
    """Terminate the hermes turn for `session` running *inside* the container.

    Killing the local `docker exec` client does not reliably stop the process
    it spawned in the container, so we pkill it by its unique `--resume <key>`
    command line. `-f` takes a REGEX, so the key going into it must contain no
    metacharacters — enforced by _valid_turn_key at every endpoint that
    accepts one, not merely assumed.
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
        last_flush = 0.0
        try:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = ANSI_RE.sub("", raw.decode(errors="replace")).rstrip("\n")
                captured.append(line)
                await q.put(("chunk", record("chunk", {"text": line})))
                # Mirror the reply to disk as it arrives. The record used to be
                # written only on poller ticks (which carry no chunks) and at
                # completion, so a webui killed mid-reply lost every token it
                # had already received. Throttled, because a fast model emits
                # lines faster than it is worth rewriting the file.
                now_ts = time.time()
                if now_ts - last_flush >= 2.0:
                    last_flush = now_ts
                    rec["text"] = "\n".join(captured)
                    _persist_turn(session, rec)
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

    # Not bound to a name: _spawn already holds the strong reference (BG_TASKS)
    # that keeps the task alive, and nothing here awaits the reader.
    _spawn(read_stdout())
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
    if not _valid_turn_key(session):
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
    if not _valid_turn_key(session):
        return JSONResponse({"error": "bad turn key"}, status_code=400)
    _drop_turn(session)
    return {"ok": True}


@app.get("/api/turns")
async def turns_index(limit: int = 40):
    """Every unacked turn record still on disk, newest first.

    This is the answer to "what survived?" after a webui or agent restart —
    replies that completed while nobody was attached are held here until a
    client acks them, and until now nothing could enumerate them: recovery
    only worked if the client still remembered the session key it had used.
    """
    limit = max(1, min(limit, 200))
    try:
        paths = sorted(TURNS_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "turns": []}, status_code=500)

    items: list[dict] = []
    for path in paths[:limit]:
        key = path.name[: -len(".json")]
        rec = TURNS.get(key) or _load_turn(key)
        if rec is None:
            continue
        text = (rec.get("text") or "").strip()
        items.append({
            "session": key,
            "status": rec.get("status", "unknown"),
            "code": rec.get("code"),
            "started": rec.get("ts0"),
            "updated": rec.get("ts"),
            # Enough to recognise the conversation without shipping the whole
            # transcript to a sidebar that only needs a label.
            "preview": text[:180],
            "chars": len(text),
            "events": len(rec.get("events") or []),
        })
    return {"turns": items, "dir": str(TURNS_DIR), "total": len(paths)}


@app.post("/api/chat")
async def chat(body: ChatBody):
    msg = body.message.strip()
    if not msg:
        return JSONResponse({"error": "empty message"}, status_code=400)
    session = body.session.strip() or "webui_default"
    # The key names a file under TURNS_DIR and is interpolated into the
    # `pkill -f` pattern /api/stop uses, so it is checked here rather than
    # trusted: "../../x" would escape the records directory, and a regex
    # metacharacter would widen what a later stop kills.
    if not _valid_turn_key(session):
        return JSONResponse({"error": "bad session key"}, status_code=400)
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
    if not _valid_turn_key(session):
        return JSONResponse({"error": "bad session key"}, status_code=400)
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


def _startup() -> None:
    """One-time setup for a real server start: report the access-control
    posture, prune stale records, and reload the ones worth keeping."""
    for warning in _audit_token_config():
        print(f"WARNING: {warning}", flush=True)
    pruned = _prune_turn_files()
    restored = _restore_turns()
    print(f"turn records: dir={TURNS_DIR} restored={restored} pruned={pruned}",
          flush=True)


if __name__ == "__main__":
    import uvicorn

    _startup()

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
