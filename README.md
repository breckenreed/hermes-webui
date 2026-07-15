# Hermes WebUI

A tiny, self-contained **web chat interface for a local [Hermes Agent](https://hermes-agent.nousresearch.com) container** — think "claude-code mini" for Hermes. Command your local agent from the browser: send prompts, watch tool calls stream, and browse/resume past sessions.

![status](https://img.shields.io/badge/hermes-webui-e07b39)

## What it does

- 💬 **Chat UI** that drives the Hermes agent and streams its output live (SSE).
- 🧵 **Session sidebar** — every Hermes CLI session is listed; click one to load its full transcript (user messages, tool calls, results, and answers).
- 🔁 **Resume any conversation** — sending a message continues that exact session, so context is preserved by Hermes' own session store.
- 🗂️ **Session actions** — per-session `⋯` menu to **Rename**, **Copy ID**, or **Delete**.
- ⏹ **Stop** — interrupt a running response mid-stream; the Send button becomes a Stop button while the agent is working.
- 🩺 **Live health** indicator showing whether the Hermes container is reachable.
- 📦 **Zero external frontend deps** — one HTML file, no CDN, works offline.

## How it works

The webui does **not** reimplement the agent. It shells into your already-running
Hermes container and runs the one-shot CLI:

```
docker exec hermes-agent hermes -z "<your prompt>" --resume <session> --yolo --cli
```

`stdout` is streamed back to the browser over Server-Sent Events. Sessions,
history, tools, and the LLM connection are all handled by Hermes itself.

```
┌────────────┐     HTTP/SSE     ┌──────────────┐   docker exec   ┌──────────────┐
│  Browser   │ ───────────────▶ │  hermes-webui │ ──────────────▶ │ hermes-agent │
│  chat UI   │ ◀─────────────── │   (FastAPI)   │ ◀────stdout──── │  (Hermes)    │
└────────────┘                  └──────────────┘                 └──────────────┘
                                        │
                                  /var/run/docker.sock
```

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

The note is wrapped in internal markers and **stripped from the chat transcript**,
so you only ever see your own messages. Override it with `HERMES_SYSTEM_PREAMBLE`
(e.g. to point at a different vault path), or set it empty to turn it off.

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
| `GET`  | `/api/sessions` | List recent CLI sessions |
| `GET`  | `/api/session/{id}` | Normalized transcript for one session |
| `POST` | `/api/chat` | Send a prompt; streams the reply as SSE. Body: `{"message","session"}` |
| `POST` | `/api/stop` | Stop the in-flight response for a session (kills the running hermes turn). Body: `{"session"}` |
| `DELETE` | `/api/session/{id}` | Delete a session |
| `POST` | `/api/session/{id}/rename` | Rename a session. Body: `{"title"}` |

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
