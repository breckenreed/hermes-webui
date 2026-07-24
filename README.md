# Hermes WebUI

A tiny, self-contained **web chat interface for a local [Hermes Agent](https://hermes-agent.nousresearch.com) container** — think "claude-code mini" for Hermes. Command your local agent from the browser: send prompts, watch tool calls stream, and browse/resume past sessions.

![status](https://img.shields.io/badge/hermes-webui-e07b39)

## What it does

- 💬 **Chat UI** that drives the Hermes agent and streams its output live (SSE).
- 🧵 **Session sidebar** — every Hermes CLI session is listed; click one to load its full transcript (user messages, tool calls, results, and answers).
- 🔁 **Resume any conversation** — sending a message continues that exact session, so context is preserved by Hermes' own session store.
- 🗂️ **Session actions** — per-session `⋯` menu to **Rename**, **Copy ID**, or **Delete**.
- ⏹ **Stop** — interrupt a running response mid-stream; the Send button becomes a Stop button while the agent is working.
- ⚡ **Agent mode** — a composer toggle for multi-step / todo work. `hermes -z` is one-shot, and weaker local models tend to plan (write a todo list) and then stop — narrating "next I'll…" instead of doing it. When on, the webui (a) adds a directive telling the model to execute its whole plan and emit `[[TASK_COMPLETE]]` only when finished, and (b) **auto-continues** — while that token is absent it re-prompts the model to run the next step, one process at a time, bounded by a round cap, so it drives itself to the end and stops after the last step. No extra sessions, no change to `--yolo`. Override the directive with `HERMES_AGENT_DIRECTIVE`.
- 🛠️ **Live tool trace** — the agent's actions stream into the chat Claude-code style: each tool call (`● write_file {...}`) and its result (`↳ …`) appears as it happens.
- 📊 **Context meter** — the top bar shows estimated token usage for the current conversation against the model's configured context window (hover for the breakdown: fixed prompt budget vs. history).
- 🕒 **Timestamps & live status** — every tool call/result is stamped `HH:MM:SS` and each reply shows a completion time. On reconnect the chat shows the *actual* current condition (`Running tool: write_file`, `Generating response…`) with elapsed time, instead of a flat "still generating".
- 🔌 **Disconnect-proof turns** — every turn is recorded server-side (memory + disk). A mobile client that locks, backgrounds, or loses wifi mid-turn reattaches on return: it sees the sub-steps completed so far while the turn is still running, gets the full reply when it finishes, and sees *"Prompt processing failed"* only after the server has checked its records, the live process, and the Hermes session store.
- 🧵➕🧵 **True multi-conversation concurrency** — every turn is tracked per conversation, not globally. Switch chats freely while a turn is running elsewhere; each one keeps streaming, recovering, and saving to *its own* history. (Earlier versions used a single global "busy"/message-list, so a turn that outlived a conversation switch — new tab, reload + immediate switch — could apply its reply to whatever chat was on screen when it finished. Fixed.)
- ⌨️ **Slash commands** — `/queue <text>` lines up a follow-up that sends automatically the moment the current turn finishes; `/steer <text>` stops the current step and immediately redirects the agent with a new instruction (plus whatever partial output it had produced) instead of waiting for it to finish; `/stop` stops the running turn. Autocompletes as you type `/`.
- 🌐 **Online model picker** — a composer button lists every entry in Hermes' `fallback_providers` config (Gemini, or anything else you've configured) as an on-demand choice, not just an automatic failover. Pick one and the next message routes through it (`-m <model> --provider <provider>` for that turn only — no config change, no restart); pick "🖥 Local" to go back. See [Online models](#online-models) below.
- 🩺 **Live health** indicator showing whether the Hermes container is reachable.
- 📦 **Zero external frontend deps** — one HTML file, no CDN, works offline.

## How it works

The webui shells into your already-running Hermes container and runs the
one-shot CLI, streaming `stdout` back to the browser over Server-Sent Events:

```
docker exec hermes-agent hermes -z "<prompt>" --resume <turn-key> --yolo --cli
```

```
┌────────────┐     HTTP/SSE     ┌──────────────┐   docker exec   ┌──────────────┐
│  Browser   │───────────────▶ │  hermes-webui │──────────────▶ │ hermes-agent │
│  chat UI   │◀─────────────── │   (FastAPI)   │◀────stdout──── │  (Hermes)    │
└────────────┘                  └──────────────┘                 └──────────────┘
                                        │
                                  /var/run/docker.sock
```

### Conversations live in the browser

`hermes -z` is **one-shot**: every invocation forks a fresh session and does
not reliably carry earlier turns forward (apparent recall comes from Hermes'
global *memory* feature, not session continuity). So the webui **owns the
conversation itself** — each chat is stored in the browser's `localStorage`,
and the full history is **injected into every prompt** as context. This gives
the model correct, explicit context on every turn, survives reloads and phone
locks, and means no session cloning. The sidebar lists *your* conversations,
not Hermes' internal per-turn session rows.

Every in-flight turn is tracked **per conversation** (`convoId -> {turnKey,
abort, ...}`), never as one global "busy" flag. That's what lets you freely
switch chats while one is still running — each turn streams, recovers, and
saves to its own conversation's storage regardless of which one is on screen
when it resolves — and it's what makes `/queue` and `/steer` possible.

### Slash commands

Type these into the composer:

| Command | What it does |
|---|---|
| `/queue <text>` | Sends `<text>` as a normal follow-up message the moment the current turn finishes. Useful for typing ahead instead of waiting. Multiple `/queue` calls append to one another. |
| `/steer <text>` | Stops the current step and immediately re-prompts the model with `<text>`, including whatever partial output/tool trace it had produced so far. `hermes -z` can't be interrupted mid-generation (it isn't reading stdin while it runs), so this is "stop + redirect with context," not true mid-stream steering — that's the honest limit of the one-shot-process architecture. |
| `/stop` | Stops the turn running in the current conversation. Same as clicking the Send button while it shows ■. |

Autocomplete appears as soon as you type `/`; arrow keys to select, Tab/Enter
to fill in the command name. Both commands work even while a turn from the
*same* conversation is already streaming (that's the point) — sending a plain
message into a busy conversation is still blocked, with a toast pointing you
at `/queue`/`/steer` instead.

## Requirements

- Docker (Docker Desktop on Windows/macOS, or Docker Engine on Linux).
- A running Hermes container (default name: `hermes-agent`).
- The webui mounts the Docker socket so it can `docker exec` into that container.

## Quick start

```bash
git clone https://github.com/breckenreed/hermes-webui.git
cd hermes-webui

# Provide the same LLM key your Hermes container uses (see ~/.hermes/.env)
cp .env.example .env
#   edit .env -> LLM_CLIENT_UID=...

docker compose up -d --build
```

Open **http://localhost:8090**.

## Configuration

Set in `docker-compose.yml` (or via environment):

| Variable | Default | Description |
|---|---|---|
| `HERMES_CONTAINER` | `hermes-agent` | Name of the running Hermes container to drive |
| `LLM_CLIENT_UID` | *(from `.env`)* | Passed through to `hermes` so the agent can reach its LLM endpoint |
| `HERMES_MODEL` | *(empty)* | Optional model override (e.g. `google/gemma-4-12b`); blank uses the Hermes default |
| `HERMES_SYSTEM_PREAMBLE` | *(built-in default)* | Short context note prepended to every prompt so small local models use their tools instead of guessing their environment. Set to an empty string to disable |
| `WEBUI_TOKEN` | *(empty = open)* | Access token required for all `/api/*` calls (`Authorization: Bearer`). Set it on any shared network |
| `WEBUI_TLS` | `0` | `1` = HTTPS with an auto-generated self-signed cert on the same port |

### System preamble

Small local models (e.g. `google/gemma-4-12b`) will sometimes answer filesystem
questions from a hallucinated self-image ("I'm a WSL instance, files are under
`/mnt/c`…") instead of actually running a tool. To counter this, the webui
prepends a short context note to every prompt:

> You are running inside a Linux container (not WSL). The user's Obsidian vault
> is bind-mounted read-write at `/host/opser-local`. Always use your tools to
> inspect or modify the filesystem — never guess about your environment or where
> files live.

The note rides at the **top of the composed prompt** (ahead of the injected
conversation history) and never appears in the chat — you only see your own
messages and the reply. Override it with `HERMES_SYSTEM_PREAMBLE` (e.g. to point
at a different vault path), or set it empty to turn it off.

Port mapping (host `8090` → container `8000`) is set in `docker-compose.yml`.

### Online models

`fallback_providers` in `~/.hermes/config.yaml` is normally an **automatic**
failover chain — Hermes only tries an entry when the primary model errors
with a rate-limit/5xx/connection failure. The webui's 🌐 model picker
repurposes the same list as an **on-demand menu**: pick an entry and your next
message routes through it directly (`-m <model> --provider <provider>` for
that turn only), instead of waiting for the local model to fail first.

Add an entry to enable it:

```yaml
fallback_providers:
  - provider: gemini          # Hermes' internal provider id — NOT the display
                               # name shown in `hermes model`'s picker ("Google
                               # AI Studio"). Run `hermes fallback add` once to
                               # see the id it writes, or check hermes_cli/auth.py.
    model: gemini-3.6-flash   # the model field is named "model" here, not
                               # "default" (that's only the top-level `model:`
                               # block's key) — get_fallback_chain() silently
                               # drops any entry missing "provider" or "model".
    api_key: ${GEMINI_CLIENT_UID}
    context_length: 1000000
    max_tokens: 16384
```

Run `hermes fallback list` inside the container to confirm Hermes actually
parsed the entry — a config with the wrong keys loads with **no error and no
entries**, which looks identical to "not configured".

The provider's own credential env var must reach the **hermes-agent**
container directly (e.g. `GEMINI_API_KEY` for the built-in `gemini` provider —
set it in `hermes-docker`'s `.env`/`environment:`, not this repo's `.env`).
An explicit `api_key:` in the fallback entry (as above) works too, but only
for Hermes' *automatic* failover path — the webui's ad-hoc `-m`/`--provider`
override reads the provider's native env var (`GOOGLE_API_KEY`/
`GEMINI_API_KEY` for `gemini`), not the fallback entry's `api_key:` field, so
set both if you want the picker and automatic failover to both work.

Setting that provider credential is unrelated to `terminal.env_passthrough` /
Hermes' sandbox credential-scrubbing guardrail (which blocks well-known
provider-key names like `GOOGLE_API_KEY` from reaching the sandboxed
`execute_code`/`terminal` child process only, per GHSA-rhgp-j443-p4rf) — that
guardrail never touches the main Hermes process's own environment, which is
what resolves provider auth for both the fallback chain and the picker.

### Docker socket path

`docker-compose.yml` mounts `//var/run/docker.sock:/var/run/docker.sock`, which
works with Docker Desktop (Windows/macOS) and Linux. On plain Linux you can use
the single-slash form `/var/run/docker.sock:/var/run/docker.sock`.

## API

The FastAPI backend also exposes a small JSON API you can script against:

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/health` | Is the Hermes container reachable? |
| `POST` | `/api/chat` | Send a turn; streams the reply as SSE. Body: `{"message","session","history":[{"role","text"}],"model","provider"}` — `session` is a unique per-turn key; `history` is the prior conversation; `model`/`provider` (optional) override Hermes' default for this turn only |
| `POST` | `/api/stop` | Stop the in-flight turn. Body: `{"session"}` (the turn key) |
| `GET`  | `/api/turn/{session}` | Reattach point for a lost turn: `{status: running\|done\|failed, events, text}`. While running, returns completed sub-steps (tool calls/results, interim messages) live; reports `failed` only after checking the record, the live process, and the Hermes session store |
| `POST` | `/api/turn/{session}/ack` | Confirm receipt of a turn's outcome; the server then drops its record. Until acked, reconnects can replay it |
| `GET`  | `/api/context` | Context-window report: `{model, context_length, base_tokens, breakdown}` — the fixed prompt budget Hermes spends before the conversation starts (from `hermes prompt-size`). Token counts estimated at ~4 chars/token. Cached 5 min |
| `GET`  | `/api/models` | Selectable models for the picker: `{primary:{model,provider}, options:[{model,provider}, ...]}` — `primary` is Hermes' configured default, `options` is the parsed `fallback_providers` chain. Cached 5 min |

## Security notes

- **Set `WEBUI_TOKEN` on any shared network.** The webui grants full agent
  access (including yolo file writes) to whoever reaches the port. With a token
  set, every `/api/*` request must carry `Authorization: Bearer <token>`; the
  browser shows a lock screen once and remembers the token. Generate one with
  `openssl rand -hex 24`.
- **Enable `WEBUI_TLS=1` on semi-public LANs.** Plain HTTP exposes the token
  and chat content to sniffing/MITM. With TLS on, the container generates a
  self-signed cert at first start (kept inside the container) and serves
  HTTPS on the same port — browsers warn once about the cert; accept it.
- **Keep `.env` out of git.** It holds your `LLM_CLIENT_UID` and `WEBUI_TOKEN`.
  This repo ships a `.gitignore` that excludes it and an `.env.example`.
- Chat runs the agent with `--yolo` (no per-tool confirmation), matching the
  hands-off "command my agent" use case.
- Mounting the Docker socket gives the container control over the Docker
  engine. This is required for `docker exec`; only run it locally.
- **The Hermes ↔ LLM leg is separate.** The webui never talks to the LLM —
  `webui ↔ hermes` is a local `docker exec` on the same machine. But Hermes
  itself calls your LM Studio server over the LAN with a bearer key; to protect
  that hop, put both machines on a [Tailscale](https://tailscale.com) tailnet
  (WireGuard-encrypted) and point Hermes' `base_url` at the tailnet address,
  or use an SSH tunnel. An LM Studio *client* install cannot act as a relay
  for other apps.

## Project layout

```
hermes-webui/
├── server.py            # FastAPI backend (exec into Hermes, stream SSE)
├── static/index.html    # Single-file chat UI (no external deps)
├── Dockerfile           # python:3.12-slim + docker-ce-cli
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## License

MIT
