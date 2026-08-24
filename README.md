# Hermes WebUI

A tiny, self-contained **web chat interface for a local [Hermes Agent](https://hermes-agent.nousresearch.com) container** — think "claude-code mini" for Hermes. Command your local agent from the browser: send prompts, watch tool calls stream, and browse/resume past sessions.

![status](https://img.shields.io/badge/hermes-webui-e07b39)
[![tests](https://github.com/breckenreed/hermes-webui/actions/workflows/tests.yml/badge.svg)](https://github.com/breckenreed/hermes-webui/actions/workflows/tests.yml)
[![docker-smoke](https://github.com/breckenreed/hermes-webui/actions/workflows/docker-smoke.yml/badge.svg)](https://github.com/breckenreed/hermes-webui/actions/workflows/docker-smoke.yml)

## What it does

- 💬 **Chat UI** that drives the Hermes agent and streams its output live (SSE).
- 🧵 **Session sidebar** — every Hermes CLI session is listed; click one to load its full transcript (user messages, tool calls, results, and answers).
- 📌 **Grouped sidebar and pinning** — conversations sit under **Today / Yesterday / Earlier**, each collapsible and remembering whether you left it shut. Pin one from the `⋯` menu and it moves to a **Pinned** section above everything else: a flat recency list means the conversation you return to daily sinks the moment you start something new. Day boundaries are built from the calendar rather than by subtracting 24 hours — across a DST change a day is 23 or 25 hours long, and fixed arithmetic puts "yesterday" in the wrong day twice a year — and the sidebar re-groups itself at midnight, so a window left open overnight does not keep calling yesterday "Today". While a search is active the grouping steps aside for a flat list of results.
- 🔁 **Resume any conversation** — sending a message continues that exact session, so context is preserved by Hermes' own session store.
- ↓ **Export a conversation** — `.md` or a self-contained `.html`, from the per-session `⋯` menu. This is not a convenience: a conversation exists in exactly one browser's `localStorage`, with no server-side copy to fall back on, so an export is the only durability this data has. The HTML export lifts the app's own stylesheet out of the live page (no second copy to drift), bakes the current theme, and hides the controls that only work with the app's JS behind them. It makes **zero network requests** when opened — verified: no `<img>`, no `src=`, no `<link>`, no `<script>`, no `url(http…)` in the CSS. Links stay links, and fetch only if you click one.
- 🔎 **Search your conversations** — filters the sidebar by title **and message text**, live. Searching titles alone would be close to useless here: a conversation is titled from the first 60 characters of its opening message, so the thing you actually remember about it ("that error about the docker socket") is almost always in the body. `⌘K`/`Ctrl+K` jumps to the box, Escape clears it. Each conversation's text is parsed once and cached, and the cache is dropped whenever that conversation changes — including when Retry or Edit *removes* text, so a search can't keep finding something you deleted.
- 🗂️ **Session actions** — per-session `⋯` menu to **Rename**, **Duplicate**, **Copy ID**, or **Delete**.
- ⧉ **Duplicate a chat (fork-from-here)** — copy a conversation at its current state and take the copy somewhere else without disturbing the original: try a different approach, or branch a long investigation two ways. It costs nothing because the webui owns conversations in `localStorage` and injects the history into each prompt — there is no server-side chat to fork, so Hermes, its session store and `--yolo` are not involved at all. The copy deliberately does **not** inherit the original’s in-flight turn: a reply still running belongs to the conversation that started it, and two chats reattaching to one turn would both write that reply into themselves.
- ⏹ **Stop** — interrupt a running response mid-stream; the Send button becomes a Stop button while the agent is working.
- ↻ **Retry & edit-and-regenerate** — every reply carries a **Retry** control that discards it and asks again; every message you sent carries **Edit**, which opens it inline and, on save, drops everything after it and re-runs from that point. Both are nearly free here: conversations live in `localStorage`, so "regenerate from message N" is a slice of an array — there is no server-side chat to rewind and no session to fork. Each starts a fresh turn key, so records and the reconnect path stay correct, and neither is offered while that conversation has a turn running or a compaction in flight. `↑` on an empty composer recalls your last message, shell-style.
- ⚡ **Agent mode** — a composer toggle for multi-step / todo work. `hermes -z` is one-shot, and weaker local models tend to plan (write a todo list) and then stop — narrating "next I'll…" instead of doing it. When on, the webui (a) adds a directive telling the model to execute its whole plan and emit `[[TASK_COMPLETE]]` only when finished, and (b) **auto-continues** — while that token is absent it re-prompts the model to run the next step, one process at a time, bounded by a round cap, so it drives itself to the end and stops after the last step. No extra sessions, no change to `--yolo`. Override the directive with `HERMES_AGENT_DIRECTIVE`.
- ☑ **Plan panel** — when a reply contains a markdown checklist, it is pinned above the transcript and updates as the turn runs, so a long multi-step turn can answer "where is it now, and what is left?" — the question Agent mode exists to make answerable, and the one the plan scrolling away used to take with it. Done items are struck through, `[~]` marks the one in progress, and the header carries the count. When the agent maintains its list through its `todo` tool, every change is picked up as it happens — the tool reports the whole list on each call, so the panel tracks it rather than showing the plan as first written. A plan written only as prose still works, parsed from the reply text. The parser is deliberately strict: a checklist is unambiguous, a numbered list of sentences is not, and guessing produces a panel that confidently shows the wrong thing — so a single stray checkbox, a numbered list, or brackets in prose all yield **no panel** rather than a bad one. It reads the active conversation's own messages, so one chat's plan cannot appear in another.
- 🛠️ **Live tool trace** — the agent's actions stream into the chat Claude-code style: each tool call (`● write_file {...}`) and its result (`↳ …`) appears as it happens.
- 🧮 **Inline LaTeX** — models write maths in prose constantly (`$\rightarrow$`, `$x^2$`, `$\leq$`) and it used to print literally, dollar signs and all. Common commands now render as Unicode (→, x², ≤, α) with no CDN or maths library, keeping the one-file/offline guarantee. Money (`$5 to $10`) and anything inside backticks or code fences are deliberately left alone, and an unrecognised command is passed through untouched rather than half-converted.
- ⧉ **Copy that works where this actually runs** — a copy control on every code block and on every reply (the reply copies its **Markdown**, not the rendered HTML, so fences and tables survive the trip). `navigator.clipboard` only exists on a *secure* origin, and this webui is routinely reached over plain http by LAN IP or through a tunnel — which is why `WEBUI_TLS` exists at all — so on exactly the setups people use most, the obvious one-liner is undefined and throws. There is a fallback for those origins, and the existing **Copy ID** action, which had the same blind spot and failed silently there, now uses it too.
- 📋 **Markdown tables** — models emit comparison/summary tables constantly; these now render as real tables (with `:---`/`---:` alignment, wrapped cells, and their own horizontal scroll) instead of a column of literal `| a | b |` lines that only read correctly if you pasted them back into a `.md` file.
- 📌 **Scroll anchoring** — the transcript follows new output only while you are already at the bottom. Scroll up mid-turn to re-read your prompt or check what the agent just did and the view stays put; a **↓ Latest** pill appears to take you back, and returning to the bottom re-arms following on its own. Previously every streamed chunk, tool event and 4s recovery poll yanked you back down, so reading anything earlier was impossible until the turn finished.
- 📊 **Context meter** — the top bar shows estimated token usage against the model's configured context window, with the two halves kept apart: `~23.1k+296 / 90k (26%)` is the fixed prompt budget (system prompt, skills index, tool schemas — set by how the agent is configured) plus the conversation itself (re-sent in full every turn, and the only half you can act on). Summed into one number the second is invisible — a 23k budget swallows a few hundred tokens of chat, so compacting a conversation in half moved the display by nothing and looked like it had failed. Warning colours still track the total, since that is what actually overflows; hover for the full breakdown.
- 🕒 **Timestamps & live status** — every tool call/result is stamped `HH:MM:SS` and each reply shows a completion time. On reconnect the chat shows the *actual* current condition (`Running tool: write_file`, `Generating response…`) with elapsed time, instead of a flat "still generating".
- 🔌 **Disconnect-proof turns** — every turn is recorded server-side (memory + disk). A mobile client that locks, backgrounds, or loses wifi mid-turn reattaches on return: it sees the sub-steps completed so far while the turn is still running, gets the full reply when it finishes, and sees *"Prompt processing failed"* only after the server has checked its records, the live process, and the Hermes session store.
- 🧵➕🧵 **True multi-conversation concurrency** — every turn is tracked per conversation, not globally. Switch chats freely while a turn is running elsewhere; each one keeps streaming, recovering, and saving to *its own* history. (Earlier versions used a single global "busy"/message-list, so a turn that outlived a conversation switch — new tab, reload + immediate switch — could apply its reply to whatever chat was on screen when it finished. Fixed.)
- ⌨️ **Slash commands** — `/queue <text>` lines up a follow-up that sends automatically the moment the current turn finishes; `/steer <text>` stops the current step and immediately redirects the agent with a new instruction (plus whatever partial output it had produced) instead of waiting for it to finish; `/stop` stops the running turn; `/compact [topic]` folds earlier turns into a summary. Autocompletes as you type `/`.
- 🗜 **Compaction** — because the webui owns the conversation and injects **all** of it into every prompt, a long chat pays for its own length on every turn: the context meter climbs even when the topic hasn't moved, and eventually the window stops fitting. `/compact` runs one tool-less turn that folds the older messages into dense notes and **replaces** them with those notes, so the thread continues from a fraction of the context instead of being abandoned for a new chat. The last exchange is kept verbatim (a summary flattens exactly the detail the next reply leans on hardest), the result is a visible, expandable anchor rather than a silent edit, and a failed compaction changes nothing — you keep the original history. See [Compaction](#compaction).
- 📎 **Attachments** — drag a file onto the composer, paste a screenshot, or pick one. The agent runs in a *different container*, so a path alone would mean nothing: the file is written to the webui's state volume and `docker cp`-ed across, and the prompt carries the in-container path so the agent opens it with the same tools it uses for anything else. Two placement rules are deliberate — the durable copy lives on the state volume rather than in a container filesystem that every rebuild discards, and the agent's copy goes to a directory of its own, **never the workspace**, because an attachment landing where the agent happens to be working turns "here is a file to look at" into an edit to the project. Filenames are rebuilt from a safe alphabet rather than sanitised (they cross two path boundaries on the way), oversized files are refused up front, and the tray survives a reload so a phone that reloads mid-compose does not silently drop what you just picked.
- 🌐 **Online model picker** — a composer button lists every entry in Hermes' `fallback_providers` config (Gemini, or anything else you've configured) as an on-demand choice, not just an automatic failover. Pick one and the next message routes through it (`-m <model> --provider <provider>` for that turn only — no config change, no restart); pick "🖥 Local" to go back. See [Online models](#online-models) below.
- ⚡ **"Use best available" + automatic rate-limit rerouting** — one click starts a turn at the top of your `fallback_providers` hierarchy and lets Hermes' own retry loop cascade through the rest of the chain in-process if a model hits a rate limit, so a single turn can survive several models' quotas being exhausted without you doing anything. Because that switch is otherwise silent (Hermes only prints it to a live console, never to the transcript), the webui detects it by diffing the session's final model against what was requested and shows a persistent, timestamped notice — `⚠️ gemini-3.6-flash was rate-limited — continued on gemini-3-flash-preview` — plus a toast at the moment it happens.
- 🩺 **Live health that means something** — the dot answers "can the agent take a turn?", not merely "is its container running?". Three states: **green** (container up and the CLI answered), **amber** (container up, agent silent — turns will fail) and **red** (container down). The old check only read `{{.State.Running}}`, so when the agent exited and its restart policy revived it, the light stayed green through the whole outage while every in-flight turn died. The CLI probe is cached so the 15s poll stays cheap, and a **restart is detected within one poll** via the container’s start time: any turn that began before it cannot have survived, so it is cut loose into the normal recovery path immediately instead of waiting out the 45s stream watchdog, and you get told it happened.
- ⚹ **MCP server visibility** — a top-bar chip shows how many configured MCP servers are healthy; clicking it lists each one's transport, connection state, credential resolution, and every tool it advertises (marking which are actually offered to the agent after `tools.include`/`exclude`). Crucially it separates *reachable* from *authenticated*: an unset `${VAR}` isn't an error to Hermes — it passes the placeholder through literally, so the server starts and lists its tools while every call 401s. That reports as amber "auth required", not green. MCP calls also render distinctly in the transcript as `⚹ server › tool`, with rejected calls flagged. See [MCP servers](#mcp-servers).
- 🎓 **Skills browser** — a chip listing every skill enabled for the next turn, grouped by category and filterable. Names only: a skill's `SKILL.md` body is read by Hermes on invocation, never by this panel, so browsing costs no context. See [Skills](#skills).
- 🌗 **Dark & light themes** — "dark roast" and "light crema" on one warm palette, toggled from the top bar, persisted, defaulting to your OS preference and stamped before first paint (no flash). Every colour is a token, and both themes are checked against WCAG AA. See [Theming](#theming).
- 📲 **Installable (PWA)** — a manifest, an icon and a small service worker, so the webui can be added to a phone's home screen and opened as an app. The worker follows two rules. **API responses are never cached**: health, sessions and turn records are the live state of an agent running right now, and a stale one is worse than an error because it looks like the truth. **Navigations are network-first**: the whole app is one HTML file served with a per-request CSP nonce, so a cache-first shell would eventually serve a page whose inline scripts no longer match the header they arrived with — the cached copy is strictly the offline fallback, which lets you still read past conversations (they live in `localStorage`) when the connection drops. The cache name is derived from the app's own mtimes, so a deploy invalidates it without anyone remembering to bump a constant. iOS ignores the manifest for the home-screen icon and does not honour SVG, so a raster `apple-touch-icon.png` ships alongside it — with square corners, because iOS applies its own mask and a rounded source would be rounded twice.
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
| `/compact [topic]` | Summarizes the earlier messages of this conversation and replaces them with the summary, freeing context. With a topic, that subject is kept in more detail than the rest. `/compress` is an alias. See [Compaction](#compaction). |

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
| `LLM_CLIENT_UID` | *(from `.env`)* | Fallback only. Passed through to `hermes` when the agent container has no key of its own — see [LLM key handling](#llm-key-handling). Prefer setting it in the **agent** container |
| `HERMES_PASS_LLM_KEY` | *(unset)* | `1` forces the key onto every `docker exec` command line, as older versions always did. Only needed when the agent container's baked-in key is stale |
| `HERMES_MODEL` | *(empty)* | Optional model override (e.g. `google/gemma-4-12b`); blank uses the Hermes default |
| `HERMES_SYSTEM_PREAMBLE` | *(built-in default)* | Short context note prepended to every prompt so small local models use their tools instead of guessing their environment. Set to an empty string to disable |
| `HERMES_COMPACT_DIRECTIVE` | *(built-in default)* | What `/compact` asks the model to produce when folding a conversation into notes |
| `HERMES_COMPACT_TIMEOUT` | `300` | Seconds a compaction may run before it is abandoned and the container-side process killed |
| `HERMES_COMPACT_MIN_CHARS` | `80` | Below this, a compaction result is rejected rather than written back as history |
| `HERMES_MAX_UPLOAD_MB` | `20` | Largest attachment accepted; anything bigger is refused with a 413 rather than failing mid-turn |
| `HERMES_AGENT_ATTACH_DIR` | `/tmp/hermes-webui-attachments` | Where attachments are placed **inside the agent container**. Deliberately not the workspace |
| `UPLOADS_DIR` | `/app/state/uploads` | The durable copy, on the state volume. Falls back to a temp dir if unwritable |
| `HERMES_MODEL_PRICES` | *(empty)* | Newline-separated `model=input,output` per **million** tokens, for `/usage`. Empty means every non-local model reports unknown rather than free. See [Usage and cost](#usage-and-cost) |
| `HERMES_TRIPWIRES` | *(built-in list)* | Newline-separated `name=regex` rules matched against tool calls. Empty string disables the feature. See [Tripwires](#tripwires) |
| `WEBUI_TOKEN` | *(empty = open)* | Access token required for all `/api/*` calls (`Authorization: Bearer`). Set it on any shared network |
| `WEBUI_TLS` | `0` | `1` = HTTPS with an auto-generated self-signed cert on the same port |
| `WEBUI_AUTH_MAX_FAILURES` | `10` | Bad tokens from one IP before it is locked out |
| `WEBUI_AUTH_LOCKOUT_SECONDS` | `60` | How long that lockout lasts |
| `TURNS_DIR` | `/app/state/turns` | Where turn records are kept. Falls back to a temp dir if unwritable |
| `TURNS_MAX_FILES` | `200` | Turn records to keep on disk (`0` = no limit) |
| `TURNS_MAX_AGE_DAYS` | `7` | Age at which a record is pruned (`0` = never) |

### System preamble

Small local models (e.g. `google/gemma-4-12b`) will sometimes answer filesystem
questions from a hallucinated self-image ("I'm a WSL instance, files are under
`/mnt/c`…") instead of actually running a tool. To counter this, the webui
prepends a short context note to every prompt:

> You are running inside a Linux container (not WSL). Always use your tools to
> inspect the filesystem and find where things actually live — never guess about
> your environment or the location of files.

The note rides at the **top of the composed prompt** (ahead of the injected
conversation history) and never appears in the chat — you only see your own
messages and the reply. Override it with `HERMES_SYSTEM_PREAMBLE`, or set it
empty to turn it off.

**It names no paths on purpose.** If you tell the model a directory exists and
it doesn't, that assertion outranks what the tools report: the model spends the
turn trying to reconcile the missing path instead of just looking. Add a path
here only if it exists *inside the agent container*, and check it first:

```bash
docker exec hermes-agent ls /host
```

Port mapping (host `8090` → container `8000`) is set in `docker-compose.yml`.

### Compaction

Owning the conversation in the browser is what makes context correct on every
turn — but it has a running cost. The whole history is re-sent with each
message, so turn 40 pays for turns 1–39 all over again. Nothing is wrong when
this happens; it is simply what "inject the history every time" means, and the
context meter is where you watch it happen.

`/compact` is the release valve. It runs **one** Hermes turn whose prompt is the
conversation plus a summarisation directive, and whose reply replaces the
messages it summarised:

```
/compact                 # fold everything except the last exchange
/compact the TLS work    # …keeping that topic in more detail than the rest
```

What it deliberately does **not** do:

- **It doesn't touch the last exchange.** A summary flattens exactly the detail
  the next reply leans on hardest. The final user message and its answer stay
  verbatim, and only what precedes them is folded.
- **It isn't silent.** The result lands in the transcript as an expandable
  `🗜 Compacted N messages` anchor. Replacing a conversation with a paraphrase
  is not something to do behind the user's back — you can open it and check
  what survived before trusting the thread to it.
- **It isn't a turn.** No turn record is written and nothing appears in
  `/api/turns`: a compaction transforms your own client state, it is not
  something anyone said. It is not resumable for the same reason — if it dies,
  you still hold the original history.
- **It can't run alongside a turn**, in either direction: a reply in flight is
  about to append to the very history being folded, and sending is refused
  while a compaction runs. If it fails, times out, or comes back empty, the
  stored conversation is untouched — you lose a model turn, never a chat.

The model is asked for notes rather than prose, and told to keep identifiers
(paths, names, versions, error text) **verbatim** — an approximated path is
worse than an omitted one, because the next turn will act on it. Override the
whole instruction with `HERMES_COMPACT_DIRECTIVE`.

**The exit code is not trusted.** The Hermes CLI reports its own failures on
stdout and still exits `0`. During development this came back as an entire
"summary":

```
API call failed after 3 retries: Connection error.
```

Written back, that sentence *replaces* the conversation it was supposed to
preserve — the worst possible outcome for a feature whose whole job is not
losing context. So the output text is what gets checked: anything that opens
with the CLI's failure vocabulary (`API call failed`, `Connection error`,
`Traceback…`, `Rate limit`, …) is refused with a 502, as is anything shorter
than `HERMES_COMPACT_MIN_CHARS`, which catches the failure lines that
vocabulary misses. The signature match is anchored to the *start* of the
output, so a conversation that is genuinely *about* an API failure still
compacts normally.

### LLM key handling

Command lines are not private. Every `docker exec` this webui issues shows up
in `ps` for any local user on the host, so a key passed as `-e K=V` is readable
by anyone with a shell there:

```
$ ps aux | grep docker
user 39741 ... docker exec -i -e LLM_CLIENT_UID=sk-lm-… hermes-agent hermes mcp test github
```

Older versions did that on **every** call — health probes, session lists, and
each chat turn. They also did it needlessly: the agent container normally
carries `LLM_CLIENT_UID` in its own environment already, so the flag was
re-injecting a value the process would have inherited anyway.

At startup the webui now asks the container once whether it has the key —
`test -n "$LLM_CLIENT_UID"`, which reports presence and never the value — and
passes the flag only if it does not. Which path you are on is printed at
startup:

```
LLM key: taken from the agent container's own environment (never on a command line)
```

```
WARNING: the agent container has no LLM_CLIENT_UID of its own, so it is passed
on every `docker exec` command line — where any local user can read it with `ps`.
```

If you see the warning, put the key in the **agent** container's environment
(that is where `hermes` reads it from anyway) and it goes away. A container
that cannot be reached at startup keeps the old behaviour on purpose: a missing
key breaks every turn, which is a worse failure than the leak this removes.

`HERMES_PASS_LLM_KEY=1` forces the old always-pass behaviour, for the one case
that needs it — the container's baked-in key is stale and the webui's newer one
has to win.

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
| `POST` | `/api/compact` | Fold a conversation into a summary the client can swap in for the turns it replaces: `{"history":[{"role","text"}],"focus","model","provider","session"}` → `{summary, chars, turns, model}`. One tool-less Hermes turn; writes **no** turn record. `focus` biases emphasis without licensing omission. 400 if there is too little history, 502 if the model returns nothing usable, 504 on timeout (the container-side process is killed) |
| `GET`  | `/api/context` | Context-window report: `{model, context_length, base_tokens, breakdown}` — the fixed prompt budget Hermes spends before the conversation starts (from `hermes prompt-size`). Token counts estimated at ~4 chars/token. Cached 5 min |
| `GET`  | `/api/models` | Selectable models for the picker: `{primary:{model,provider}, options:[{model,provider,context_length,max_tokens}, ...]}` — `primary` is Hermes' configured default, `options` is the parsed `fallback_providers` chain in order. Cached 5 min |
| `GET`  | `/api/mcp` | MCP servers, health and discovered tools: `{servers:[{name,transport,target,state,connected,connect_ms,detail,tools,selected_tools,selected_prefixed,env_refs,missing_env}], summary}`. `?refresh=1` forces a re-probe. Cached 60s |
| `GET`  | `/api/turns` | Every unacked turn record still on disk, newest first: `{turns:[{session,status,preview,chars,events,started,updated}], dir, total}`. What survived a restart |
| `GET`  | `/api/skills` | Skills enabled for the next turn: `{skills:[{name,category,source,trust,status}], total, categories, sources}`. Names only — no `SKILL.md` is ever read. `?refresh=1` bypasses the 5 min cache |

## Turn records

Mobile clients drop constantly — a locked phone, a backgrounded browser, a wifi
blip. So the server, not the SSE stream, is the source of truth for a turn: every
event is recorded and mirrored to disk, and a reconnecting client replays the
record instead of losing the reply. `/api/turn/{session}` is the reattach point;
`/api/turns` lists what is being held.

Records live in a **named Docker volume** (`hermes-webui-state` → `/app/state`).
This matters: they used to sit under `/tmp` *inside the container*, so every
`docker compose up -d --build` silently discarded them — which defeats the point
of persisting them. If you are upgrading from an older checkout, the new volume
starts empty; nothing is migrated.

Writes are atomic (staged to a sibling file, then renamed), so a webui killed
mid-write leaves the previous record intact rather than a truncated one. The
reply text is flushed to disk as it streams, not only at completion. Retention
is bounded by `TURNS_MAX_FILES` and `TURNS_MAX_AGE_DAYS`; a record is deleted
outright once the client acks it.

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

## Usage and cost

`/usage` toggles token counts and an estimated cost, per reply and per
conversation. Off by default.

It is worth more here than in most chat UIs, because of how this webui works:
the whole history is re-sent on **every** turn, so a conversation's cost grows
with the square of its length rather than linearly. That is invisible until a
bill arrives — and it is what `/compact` exists to cut.

**Everything shown is an estimate, and is labelled as one.** The Hermes CLI
reports no usage figures, so token counts come from the same ~4-chars-per-token
approximation the context meter uses.

**No prices are built in.** They change, they vary by region and tier, and a
number baked into this repo would be wrong for somebody the day it was written
— and a confidently wrong cost is worse than none, because it gets believed and
budgeted against. So you supply the ones you actually pay:

```yaml
environment:
  - |
    HERMES_MODEL_PRICES=some-model=0.30,2.50
    another-model=1.00,3.00
```

Values are per **million** tokens, `input,output`. The key matches as a
case-insensitive substring, so a family prefix covers its variants.

Anything without a configured price reports **`—`**, never `0`. The
conversation total then reads e.g. `free+?` — the `+?` says plainly that some
turns are unaccounted for, rather than folding the unknown in as zero.

The local model is **free by construction** — it runs on hardware you already
paid for — so a turn with no model override costs nothing and says `free`.

## Tripwires

**These fire after the fact. They are not an approval gate, and nothing here
prevents a command from running.**

`hermes -z --yolo` cannot be paused to ask a question — it is not reading stdin
while it runs — and a tool call only becomes visible to the webui once Hermes'
session store has recorded it, which is to say once it has already executed. So
a tripwire is a smoke alarm, not a lock: it tells you the agent did something
you flagged, and stops the turn before whatever it was going to do next.

On a match the turn is killed (locally and inside the container) and the
transcript gets a card naming the rule and the text that matched, with a
**Continue anyway** button that re-runs the turn with *that one rule* switched
off and every other rule still armed — the point is getting past a single false
positive, not disarming the feature.

The default list is deliberately short, because a long one trains people to
click through the card, and a tripwire that is always ignored is worse than
none:

| Rule | Catches |
|---|---|
| `recursive-delete` | `rm -rf`, `rm -fr` |
| `force-push` | `git push --force`, `git push -f` |
| `pipe-to-shell` | `curl … \| sh`, `wget … \| bash` |
| `read-environment` | `/proc/<pid>/environ`, `printenv` |
| `disk-write` | `dd if=`, `mkfs`, `> /dev/sd…` |
| `credential-files` | `.ssh/id_*`, `.aws/credentials`, `.docker/config.json` |

`read-environment` is not hypothetical. Asked which port the webui listens on,
the agent went looking, ran `cat /proc/<pid>/environ`, and put the container's
whole credential set into the transcript — which is also why
[transcripts are redacted](#security-notes).

Only **tool calls** are matched, never results or the model's prose: a rule
that fired on the agent reading *about* a command is how a detector becomes
noise. Override the whole list with `HERMES_TRIPWIRES` (newline-separated
`name=regex`); an empty value turns the feature off. A rule with a bad regex is
skipped with a startup warning rather than taking the others down with it.

## Security notes

- **Transcripts are scrubbed of credential-shaped text — as damage limitation,
  not as credential security.** Every turn runs with `--yolo`, and the agent
  container typically carries a set of long-lived API tokens in its
  environment. That is not hypothetical: asked which port the webui listens on,
  the agent went looking, ran `cat /proc/<pid>/environ`, and rendered the whole
  credential set into the transcript — from there into the browser's
  `localStorage` and into a turn record on disk that outlives
  `docker compose up -d --build`. No attacker, no crafted prompt; it did that
  while trying to answer.

  Streamed output, tool call arguments, tool results and reloaded transcripts
  now pass through a redactor before they are shown or stored. It catches
  provider-shaped tokens (`ghp_`, `github_pat_`, `sk-`, `xox…`, `tvly-`,
  `AKIA…`, PEM private-key headers) and, more usefully, any `NAME=value` whose
  name looks like a credential — which is what catches an `environ` dump
  wholesale, including token shapes nobody anticipated.

  The variable name is kept (`GITHUB_TOKEN=<redacted>`) on purpose: a log that
  quietly loses text is its own debugging problem, and you need to be able to
  tell *"the agent saw nothing"* from *"the log is hiding it"*. Expect
  occasional false positives — a conversation genuinely about a config file
  will get redacted — which is the right trade for something that persists.

  **This does not make the credentials safe.** They are still sitting where the
  agent can read them; redaction only stops them being written down again. The
  actual fix is not to put them there.

- **Optional: restrict where the agent may connect** — see
  [`docs/egress.md`](docs/egress.md). `docker-compose.egress.yml` puts the agent
  on an `internal` network whose only way out is a proxy holding a list of
  approved hosts. It makes no attempt to judge what the agent is doing, which is
  the point: marking fetched content as untrusted is a priority in the prompt,
  not a boundary, and this works whether or not the model respected it. Reducing
  the list to the inference server alone is the deliberate full-isolation setup.
  It does **not** protect the destinations that are on the list — an injected
  "push this to GitHub" still works if GitHub is allowed — so narrow token
  scopes remain the other half.

- **Set `WEBUI_TOKEN` on any shared network.** The webui grants full agent
  access (including yolo file writes) to whoever reaches the port. With a token
  set, every `/api/*` request must carry `Authorization: Bearer <token>`; the
  browser shows a lock screen once and remembers the token. Generate one with
  `openssl rand -hex 24`. The server prints a warning at startup if the token
  is missing, too short, or travelling without TLS.
- **Failed logins are throttled.** The token is one shared secret with no
  account behind it, so an unthrottled endpoint is a plain guessing oracle.
  After `WEBUI_AUTH_MAX_FAILURES` bad tokens an IP is refused with `429` for
  `WEBUI_AUTH_LOCKOUT_SECONDS`. `X-Forwarded-For` is deliberately ignored —
  honouring it would let one client mint a fresh identity per guess.
- **Responses carry a strict CSP.** `script-src` uses a per-response nonce and
  no `'unsafe-inline'`, so an injected `<script>` or `onerror=` handler cannot
  execute; `connect-src 'self'` means an exfiltration beacon has nowhere to
  send. This matters because the chat pane renders model output as HTML, and
  that output routinely quotes web pages and files the agent just read.
  Alongside it: `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
  no-referrer`, a restrictive `Permissions-Policy`, `no-store` on `/api/*`,
  and HSTS when `WEBUI_TLS=1`.
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
  that hop, keep both machines on the same private, encrypted network — a
  WireGuard mesh or an SSH tunnel — and point Hermes' `base_url` at that
  address rather than at a bare LAN IP. An LM Studio *client* install cannot
  act as a relay for other apps.

## Tests

Every push and pull request runs them on GitHub Actions —
[`tests.yml`](.github/workflows/tests.yml) on Python 3.12 (what the image
ships) and 3.13 (what development runs on), plus
[`docker-smoke.yml`](.github/workflows/docker-smoke.yml), which builds the
image and checks the container actually serves. The unit suite never touches
the Dockerfile or the entrypoint, so without that second job a broken base
image or a botched entrypoint would ship behind a green test run.

The suite is hermetic — it never spawns a real subprocess and never needs the
agent container running, so it takes under a second. A test that reaches for
docker fails loudly rather than quietly shelling out.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

```bash
.venv/bin/pytest
```

Coverage is aimed at the two places bugs actually live: the parsers that scrape
Hermes' human-readable CLI output (no `--json` mode exists, so an upstream
formatting change breaks them silently), and the access-control and persistence
paths, where a failure is invisible until it matters.

Nothing here ships in the image — the Dockerfile installs `requirements.txt`
alone, so the runtime stays at four packages.

## Project layout

```
hermes-webui/
├── server.py            # FastAPI backend (exec into Hermes, stream SSE)
├── static/index.html    # Single-file chat UI (no external deps)
├── tests/               # pytest suite (dev only — not in the image)
├── Dockerfile           # python:3.12-slim + docker-ce-cli
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

## License

MIT
