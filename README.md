# Hermes WebUI

A tiny, self-contained **web chat interface for a local [Hermes Agent](https://hermes-agent.nousresearch.com) container** — think "claude-code mini" for Hermes. Command your local agent from the browser: send prompts, watch tool calls stream, and browse/resume past sessions.

![status](https://img.shields.io/badge/hermes-webui-e07b39)

## What it does

- 💬 **Chat UI** that drives the Hermes agent and streams its output live (SSE).
- 🧵 **Session sidebar** — every Hermes CLI session is listed; click one to load its full transcript (user messages, tool calls, results, and answers).
- 🔁 **Resume any conversation** — sending a message continues that exact session, so context is preserved by Hermes' own session store.
- 🗂️ **Session actions** — per-session `⋯` menu to **Rename**, **Duplicate**, **Copy ID**, or **Delete**.
- ⧉ **Duplicate a chat (fork-from-here)** — copy a conversation at its current state and take the copy somewhere else without disturbing the original: try a different approach, or branch a long investigation two ways. It costs nothing because the webui owns conversations in `localStorage` and injects the history into each prompt — there is no server-side chat to fork, so Hermes, its session store and `--yolo` are not involved at all. The copy deliberately does **not** inherit the original’s in-flight turn: a reply still running belongs to the conversation that started it, and two chats reattaching to one turn would both write that reply into themselves.
- ⏹ **Stop** — interrupt a running response mid-stream; the Send button becomes a Stop button while the agent is working.
- ⚡ **Agent mode** — a composer toggle for multi-step / todo work. `hermes -z` is one-shot, and weaker local models tend to plan (write a todo list) and then stop — narrating "next I'll…" instead of doing it. When on, the webui (a) adds a directive telling the model to execute its whole plan and emit `[[TASK_COMPLETE]]` only when finished, and (b) **auto-continues** — while that token is absent it re-prompts the model to run the next step, one process at a time, bounded by a round cap, so it drives itself to the end and stops after the last step. No extra sessions, no change to `--yolo`. Override the directive with `HERMES_AGENT_DIRECTIVE`.
- 🛠️ **Live tool trace** — the agent's actions stream into the chat Claude-code style: each tool call (`● write_file {...}`) and its result (`↳ …`) appears as it happens.
- 🧮 **Inline LaTeX** — models write maths in prose constantly (`$\rightarrow$`, `$x^2$`, `$\leq$`) and it used to print literally, dollar signs and all. Common commands now render as Unicode (→, x², ≤, α) with no CDN or maths library, keeping the one-file/offline guarantee. Money (`$5 to $10`) and anything inside backticks or code fences are deliberately left alone, and an unrecognised command is passed through untouched rather than half-converted.
- 📋 **Markdown tables** — models emit comparison/summary tables constantly; these now render as real tables (with `:---`/`---:` alignment, wrapped cells, and their own horizontal scroll) instead of a column of literal `| a | b |` lines that only read correctly if you pasted them back into a `.md` file.
- 📊 **Context meter** — the top bar shows estimated token usage for the current conversation against the model's configured context window (hover for the breakdown: fixed prompt budget vs. history).
- 🕒 **Timestamps & live status** — every tool call/result is stamped `HH:MM:SS` and each reply shows a completion time. On reconnect the chat shows the *actual* current condition (`Running tool: write_file`, `Generating response…`) with elapsed time, instead of a flat "still generating".
- 🔌 **Disconnect-proof turns** — every turn is recorded server-side (memory + disk). A mobile client that locks, backgrounds, or loses wifi mid-turn reattaches on return: it sees the sub-steps completed so far while the turn is still running, gets the full reply when it finishes, and sees *"Prompt processing failed"* only after the server has checked its records, the live process, and the Hermes session store.
- 🧵➕🧵 **True multi-conversation concurrency** — every turn is tracked per conversation, not globally. Switch chats freely while a turn is running elsewhere; each one keeps streaming, recovering, and saving to *its own* history. (Earlier versions used a single global "busy"/message-list, so a turn that outlived a conversation switch — new tab, reload + immediate switch — could apply its reply to whatever chat was on screen when it finished. Fixed.)
- ⌨️ **Slash commands** — `/queue <text>` lines up a follow-up that sends automatically the moment the current turn finishes; `/steer <text>` stops the current step and immediately redirects the agent with a new instruction (plus whatever partial output it had produced) instead of waiting for it to finish; `/stop` stops the running turn. Autocompletes as you type `/`.
- 🌐 **Online model picker** — a composer button lists every entry in Hermes' `fallback_providers` config (Gemini, or anything else you've configured) as an on-demand choice, not just an automatic failover. Pick one and the next message routes through it (`-m <model> --provider <provider>` for that turn only — no config change, no restart); pick "🖥 Local" to go back. See [Online models](#online-models) below.
- ⚡ **"Use best available" + automatic rate-limit rerouting** — one click starts a turn at the top of your `fallback_providers` hierarchy and lets Hermes' own retry loop cascade through the rest of the chain in-process if a model hits a rate limit, so a single turn can survive several models' quotas being exhausted without you doing anything. Because that switch is otherwise silent (Hermes only prints it to a live console, never to the transcript), the webui detects it by diffing the session's final model against what was requested and shows a persistent, timestamped notice — `⚠️ gemini-3.6-flash was rate-limited — continued on gemini-3-flash-preview` — plus a toast at the moment it happens.
- 🩺 **Live health** indicator showing whether the Hermes container is reachable.
- ⚹ **MCP server visibility** — a top-bar chip shows how many configured MCP servers are healthy; clicking it lists each one's transport, connection state, credential resolution, and every tool it advertises (marking which are actually offered to the agent after `tools.include`/`exclude`). Crucially it separates *reachable* from *authenticated*: an unset `${VAR}` isn't an error to Hermes — it passes the placeholder through literally, so the server starts and lists its tools while every call 401s. That reports as amber "auth required", not green. MCP calls also render distinctly in the transcript as `⚹ server › tool`, with rejected calls flagged. See [MCP servers](#mcp-servers).
- 🎓 **Skills browser** — a chip listing every skill enabled for the next turn, grouped by category and filterable. Names only: a skill's `SKILL.md` body is read by Hermes on invocation, never by this panel, so browsing costs no context. See [Skills](#skills).
- 🌗 **Dark & light themes** — "dark roast" and "light crema" on one warm palette, toggled from the top bar, persisted, defaulting to your OS preference and stamped before first paint (no flash). Every colour is a token, and both themes are checked against WCAG AA. See [Theming](#theming).
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
failover chain — Hermes only tries an entry when the current model errors
with a rate-limit/5xx/connection failure. The webui's 🌐 model picker
repurposes the same list as an **on-demand menu**: pick an entry and your next
message routes through it directly (`-m <model> --provider <provider>` for
that turn only), instead of waiting for the local model to fail first.

List **more than one** entry and it becomes a real hierarchy, not just a
single alternate — free-tier daily quotas (RPD) are often small (e.g. 20
requests/day per model on a fresh Google AI Studio project) and get exhausted
fast, so spreading turns across several models multiplies your effective
daily budget:

```yaml
fallback_providers:
  - provider: gemini
    model: gemini-3.6-flash        # best/newest first
    api_key: ${GEMINI_CLIENT_UID}
    context_length: 250000         # match your plan's real TPM ceiling, not
    max_tokens: 16384               # the model's theoretical max context —
                                     # check your usage dashboard for the
                                     # actual figure (Google AI Studio: TPM
                                     # column). A window bigger than what your
                                     # plan allows just means the meter/budget
                                     # lies about how much room is left.
  - provider: gemini
    model: gemini-3.5-flash
    api_key: ${GEMINI_CLIENT_UID}
    context_length: 250000
    max_tokens: 16384
  - provider: gemini
    model: gemini-3.1-flash-lite   # a "lite" sibling often has a MUCH higher
    api_key: ${GEMINI_CLIENT_UID}  # daily quota (500 vs 20 RPD, on the same
    context_length: 250000         # account) — put one or two near the end
    max_tokens: 16384               # of the chain as a high-headroom safety net.
```

Run `hermes fallback list` inside the container to confirm Hermes actually
parsed every entry — a config with the wrong keys loads with **no error and
no entries**, which looks identical to "not configured". `provider:` must be
Hermes' internal id (e.g. `gemini`), not the display name shown in `hermes
model`'s picker ("Google AI Studio"); each entry needs its own `model:` key
(not `default:`, which is only the top-level `model:` block's key) — an entry
missing either is silently dropped by `get_fallback_chain()`. Not every model
name your plan's dashboard lists is actually callable — an older generation
can 404 as "no longer available to new users" even while its quota still
shows on the page; verify each entry with a direct call before relying on it.

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

#### "Use best available" — automatic mid-turn rerouting

Picking **⚡ Use best available** in the model picker starts the turn at
entry #1 of your `fallback_providers` chain. If that model rate-limits (or
hits a billing/connection error), you do **not** need to resend anything:
Hermes' own retry loop (`agent.chat_completion_helpers.try_activate_
fallback`) swaps the model in-process and keeps generating the *same* reply —
verified end-to-end with a saturated top-of-chain model, where a single turn
transparently walked `gemini-3.6-flash → gemini-3.5-flash → gemini-3-flash-
preview` and still returned the correct answer.

That switch is otherwise **silent** — Hermes prints it only to a live
console (`⚠️ Rate limited — switching to fallback provider...`), never into
the session transcript. The webui detects it itself, by diffing the turn's
final model (from `hermes sessions export`) against what was requested, and
renders a persistent, timestamped notice in the reply:

> ⚠️ gemini-3.6-flash was rate-limited — continued on gemini-3-flash-preview

plus a toast at the moment it happens. Picking a *specific* model from the
list (instead of "best available") behaves the same way if THAT model itself
has further entries below it in the chain — "best available" is just a
convenience that always points at whichever entry is currently #1, so it
keeps working if you reorder the list later.

`agent.api_max_retries` (config.yaml, default 3) controls how many times
Hermes retries the *current* model before giving up and advancing to the
next one — lower it if you'd rather move through the chain faster than wait
out repeated backoff on a model that's clearly exhausted for the day.

### Docker socket path

`docker-compose.yml` mounts `//var/run/docker.sock:/var/run/docker.sock`, which
works with Docker Desktop (Windows/macOS) and Linux. On plain Linux you can use
the single-slash form `/var/run/docker.sock:/var/run/docker.sock`.

## API

The FastAPI backend also exposes a small JSON API you can script against:

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/health` | Is the Hermes container reachable? |
| `POST` | `/api/chat` | Send a turn; streams the reply as SSE (`chunk`/`tool`/`model_switch`/`error`/`ping`/`done`). Body: `{"message","session","history":[{"role","text"}],"model","provider"}` — `session` is a unique per-turn key; `history` is the prior conversation; `model`/`provider` (optional) override Hermes' default for this turn only. A `model_switch` event (`{from,to,ts}`) fires if Hermes' own fallback chain swapped models mid-turn |
| `POST` | `/api/stop` | Stop the in-flight turn. Body: `{"session"}` (the turn key) |
| `GET`  | `/api/turn/{session}` | Reattach point for a lost turn: `{status: running\|done\|failed, events, text}`. While running, returns completed sub-steps (tool calls/results, interim messages) live; reports `failed` only after checking the record, the live process, and the Hermes session store |
| `POST` | `/api/turn/{session}/ack` | Confirm receipt of a turn's outcome; the server then drops its record. Until acked, reconnects can replay it |
| `GET`  | `/api/context` | Context-window report: `{model, context_length, base_tokens, breakdown}` — the fixed prompt budget Hermes spends before the conversation starts (from `hermes prompt-size`). Token counts estimated at ~4 chars/token. Cached 5 min |
| `GET`  | `/api/models` | Selectable models for the picker: `{primary:{model,provider}, options:[{model,provider,context_length,max_tokens}, ...]}` — `primary` is Hermes' configured default, `options` is the parsed `fallback_providers` chain in order. Cached 5 min |
| `GET`  | `/api/mcp` | MCP servers, health and discovered tools: `{servers:[{name,transport,target,state,connected,connect_ms,detail,tools,selected_tools,selected_prefixed,env_refs,missing_env}], summary}`. `?refresh=1` forces a re-probe. Cached 60s |
| `GET`  | `/api/skills` | Skills enabled for the next turn: `{skills:[{name,category,source,trust,status}], total, categories, sources}`. Names only — no `SKILL.md` is ever read. `?refresh=1` bypasses the 5 min cache |

## MCP servers

Hermes loads MCP servers from `mcp_servers` in `~/.hermes/config.yaml` and exposes
their tools to the agent as `mcp__<server>__<tool>`. Without a UI for it, a
misconfigured or unauthenticated server is invisible — the only symptom is the
agent quietly failing mid-turn.

The **MCP chip** in the top bar shows `MCP <healthy>/<total>` with a status dot;
clicking it opens a panel listing, per server: transport and command/URL,
connection state and connect time, which `${VAR}` credentials resolve, and every
tool the server advertises — marking which ones survive the server's
`tools.include`/`tools.exclude` filter and are actually offered to the agent.

### When a server won't start

A stdio MCP server is spawned through a parent-death watchdog, and that wrapper
turns a failed launch into a **closed pipe** rather than an error the client can
describe. Hermes' own `missing executable` message can therefore never fire, and
the panel used to show a bare *"Connection closed"* with nothing to act on. Three
things now close that gap:

- **Missing-command preflight.** Before running the connection test, the backend
  checks that the server's `command` actually resolves inside the agent
  container. If it doesn't, the state is **"not installed"** — its own state, not
  a flavour of *unreachable*, because the fix is different — reported in about a
  second instead of burning the full `connect_timeout`. Nearly always this means
  a stale image: `docker compose up -d` reuses the existing one, so a server
  added to the Dockerfile after the last `--build` was never installed.
- **Server stderr.** Hermes writes each stdio server's output to
  `~/.hermes/logs/mcp-stderr.log`, tagged per launch. When a server is failing,
  the panel shows that launch's tail — for a masked `ENOENT` it is the only
  record of the real traceback anywhere in the system.
- **One retry on transient errors.** "Connection closed"/timeout-shaped failures
  are retried once, so a startup race doesn't paint the panel red. A genuine
  misconfiguration fails identically twice and still reports.

### Auto-recovery

Hermes only reconnects a dead stdio server on its own when it considers that
server *recycled stdio*, and that requires an idle or lifetime limit to be
configured. With neither set — the default — a server that dies mid-run stays
dead for the rest of that `hermes -z` process, and every later tool call fails
against a corpse. The panel now states this per server, and it is armed with:

```yaml
mcp_servers:
  your_server:
    keepalive_interval: 60       # notice a dropped pipe promptly (floored at 5s)
    idle_timeout_seconds: 1800   # recycle after 30m idle; next call respawns it
    max_lifetime_seconds: 43200  # hard ceiling regardless of traffic
```


Health deliberately distinguishes three states rather than up/down:

| State | Meaning |
|---|---|
| `ok` (green) | Connected, and every referenced credential resolves |
| `auth required` (amber) | Reachable, but a `${VAR}` it needs is unset — or the connection failed with an auth-shaped error |
| `unreachable` (red) | The connection failed for any other reason |

The amber state matters because an unset `${VAR}` is **not** an error to Hermes:
it leaves the placeholder literal, so the server still starts and still
advertises its full tool list, and the failure only lands later as a 401 on the
first real API call. Green would be a lie there.

In the transcript, MCP calls render as `⚹ <server> › <tool>` in a distinct
colour so an external side effect is never mistaken for a local builtin, and a
tool result that comes back rejected is flagged in red with a pointer to the panel.

> Health probing runs `hermes mcp test` per server, which costs seconds each (and
> blocks until `connect_timeout` on a dead one), so results are cached and polled
> slowly. Use **Re-check** in the panel for an answer on demand.

## Skills

The **Skills chip** opens a searchable list of every skill enabled for the next
turn, grouped by category, sourced from `hermes skills list --enabled-only`.

This is intentionally names-only. Hermes puts a skills *index* (name plus a
one-line description) in every system prompt and reads a skill's `SKILL.md` body
only when the model actually invokes it — this panel sits on the index side of
that boundary, so browsing what the agent can do costs no context and no disk
reads. The panel reports the index's context cost using the figure `/api/context`
already computes, so opening it adds no work on the agent side either.

## Theming

Two palettes on one warm axis, toggled with the ☾/☀ button in the top bar:
**dark roast** (default) and **light crema**. The choice persists in
`localStorage`; with none saved, the OS `prefers-color-scheme` decides. The
theme is stamped onto `<html data-theme>` by a tiny inline script in `<head>`
so there is no flash of the wrong palette before first paint.

Every colour is a CSS custom property defined once per theme in
`static/index.html` — no rule hardcodes one, which is what keeps the two themes
from drifting as features are added. Names are semantic (`--dim`, `--line`,
`--on-accent`) rather than literal, because the palettes invert: `--accent2` is
*lighter* than `--accent` in dark and *darker* in light, since in both cases it
means "accent used as text". Both themes are checked against WCAG AA (4.5:1 for
body text) — the light theme's accent is deeper than the dark theme's for
exactly that reason.

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
