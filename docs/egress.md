# Egress control for the agent

Restricting where the agent container may connect, so that a tool call it was
talked into making cannot send anything to a destination you did not approve.

## Why this and not a prompt filter

Every turn runs with `--yolo`, and the agent reads text other people wrote:
ticket descriptions, documents, fetched web pages. Marking that content as an
untrusted tool result helps and is worth doing — but it is a **priority in the
prompt, not a boundary**. Instruction-shaped text sits in the same context
window and competes with the marking, and how reliably a model holds that
hierarchy is one of the first properties to degrade with smaller and quantized
models.

So the control worth having is one that does not depend on the model having
obeyed anything. This is that: it makes no attempt to judge what the agent is
doing, and answers a single question — *is this destination on the list the
operator approved?*

## How it works

```
┌────────────────────┐        ┌──────────────────┐       ┌──────────┐
│   hermes-agent     │        │  hermes-egress   │       │ approved │
│                    │──────▶│  (tinyproxy,     │─────▶│  hosts   │
│  HTTP(S)_PROXY set │        │   allowlist)     │       └──────────┘
└────────────────────┘        └──────────────────┘
   network: hermes-egress          also on: hermes-egress-out
   internal: true  ── no route off the host at all
```

**The enforcement is the network, not the proxy.** `hermes-egress` is marked
`internal`, so containers on it have no route off the host. The proxy is simply
the only thing that also sits on a network with a way out. A tool that ignores
`HTTP_PROXY` therefore fails to connect rather than slipping past the
allowlist — fail-closed, not advisory.

## Bringing it up

```bash
docker compose -f docker-compose.yml -f docker-compose.egress.yml up -d
```

That builds and starts the proxy and creates both networks. It does **not**
touch the agent — `hermes-agent` belongs to its own compose project, so joining
it is one edit on that side:

```yaml
services:
  hermes-agent:
    # ONLY this network. Leaving the old one attached leaves the old route out,
    # and everything below becomes decoration.
    networks:
      - hermes-egress
    environment:
      - HTTP_PROXY=http://hermes-egress:8888
      - HTTPS_PROXY=http://hermes-egress:8888
      # Lowercase too: some clients read only one spelling, and a client that
      # reads neither will simply fail to connect, which is the point.
      - http_proxy=http://hermes-egress:8888
      - https_proxy=http://hermes-egress:8888
      - NO_PROXY=localhost,127.0.0.1

networks:
  hermes-egress:
    external: true
```

The webui is unaffected: it reaches the agent over the Docker socket, not the
network.

## Adding a destination

Edit [`egress/allowlist.txt`](../egress/allowlist.txt) and restart the proxy.
No code change, no rebuild of anything else.

```bash
docker compose -f docker-compose.egress.yml up -d --build hermes-egress
```

**Anchor every entry.** `github\.com$` also matches `evil-github.com`. The
`^…$` pair is the difference between an allowlist and a suggestion — see the
lookalike test below.

## Full isolation

Same file, allowlist reduced to the inference server alone. The agent can then
reach the model and nothing else. This is the "deliberate complete isolation"
option: it needs no separate mode, just a shorter list.

## What this does not buy

- **It does not protect the destinations that are on the list.** An injected
  "push this to GitHub" still works if GitHub is allowed. Egress control bounds
  *where* things can go, not *what* is sent there. Narrow token scopes are the
  other half — fine-grained PATs limited to the repositories that person needs,
  and PR-only rather than write-to-`main` where possible.
- **It is not a sandbox.** The agent still runs arbitrary commands inside its
  container with `--yolo`.
- **It assumes the agent has no Docker socket.** A container that can talk to
  the engine can start another container on an unrestricted network. The webui
  mounts the socket; the agent must not.

## Verifying it

Run these against the proxy after any allowlist change. Each one is a different
way the control can be quietly wrong:

```bash
# 1. an allow-listed host works                     → expect 200
docker run --rm --network hermes-egress curlimages/curl -s -o /dev/null \
  -w '%{http_code}\n' -x http://hermes-egress:8888 https://api.github.com/zen

# 2. an arbitrary host does not                     → expect 000 + "refused" in the log
docker run --rm --network hermes-egress curlimages/curl -s -o /dev/null \
  -w '%{http_code}\n' -x http://hermes-egress:8888 https://example.com

# 3. a lookalike host does not (anchoring)          → expect 000
docker run --rm --network hermes-egress curlimages/curl -s -o /dev/null \
  -w '%{http_code}\n' -x http://hermes-egress:8888 https://api.github.com.attacker.test

# 4. an allowed host on another port does not       → expect 000, "Refused CONNECT method on port 22"
docker run --rm --network hermes-egress curlimages/curl -s -o /dev/null \
  -w '%{http_code}\n' -x http://hermes-egress:8888 https://api.github.com:22

# 5. no proxy at all → no route (fail-closed)       → expect a connect failure, not a 200
docker run --rm --network hermes-egress curlimages/curl -s -o /dev/null \
  -w '%{http_code}\n' -m 10 https://api.github.com/zen

docker logs hermes-egress | tail -20
```

Test 4 matters more than it looks: without `ConnectPort 443` the proxy would
tunnel *any* port on an allow-listed host — SSH, a database — which is a much
larger hole than the allowlist implies.
