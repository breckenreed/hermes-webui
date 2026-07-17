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
- 🔌 **Disconnect-proof turns** — every turn is recorded server-side (memory + disk). A mobile client that locks, backgrounds, or loses wifi mid-turn reattaches on return: it sees the sub-steps completed so far while the turn is still running, gets the full reply when it finishes, and sees *"Prompt processing failed"* only after the server has checked its records, the live process, and the Hermes session store.
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
│  Browser   │ ───────────────▶ │  hermes-webui │ ──────────────▶ │ hermes-agent │
│  chat UI   │ ◀─────────────── │   (FastAPI)   │ ◀────stdout──── │  (Hermes)    │
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

### Docker socket path

`docker-compose.yml` mounts `//var/run/docker.sock:/var/run/docker.sock`, which
works with Docker Desktop (Windows/macOS) and Linux. On plain Linux you can use
the single-slash form `/var/run/docker.sock:/var/run/docker.sock`.

## API

The FastAPI backend also exposes a small JSON API you can script against:

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/health` | Is the Hermes container reachable? |
| `POST` | `/api/chat` | Send a turn; streams the reply as SSE. Body: `{"message","session","history":[{"role","text"}]}` — `session` is a unique per-turn key; `history` is the prior conversation |
| `POST` | `/api/stop` | Stop the in-flight turn. Body: `{"session"}` (the turn key) |
| `GET`  | `/api/turn/{session}` | Reattach point for a lost turn: `{status: running\|done\|failed, events, text}`. While running, returns completed sub-steps (tool calls/results, interim messages) live; reports `failed` only after checking the record, the live process, and the Hermes session store |
| `POST` | `/api/turn/{session}/ack` | Confirm receipt of a turn's outcome; the server then drops its record. Until acked, reconnects can replay it |
| `GET`  | `/api/context` | Context-window report: `{model, context_length, base_tokens, breakdown}` — the fixed prompt budget Hermes spends before the conversation starts (from `hermes prompt-size`). Token counts estimated at ~4 chars/token. Cached 5 min |

## Security notes

- **Keep `.env` out of git.** It holds your `LLM_CLIENT_UID`. This repo ships a
  `.gitignore` that excludes it and an `.env.example` to copy from.
- Chat runs the agent with `--yolo` (no per-tool confirmation), matching the
  hands-off "command my agent" use case. Run the webui only on trusted/local
  networks; it grants full agent access to anyone who can reach the port.
- Mounting the Docker socket gives the container control over the Docker engine.
  This is required for `docker exec`; only run it locally.

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
