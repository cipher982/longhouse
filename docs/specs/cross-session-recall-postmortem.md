# Cross-session recall: postmortem and redesign

**Date:** 2026-07-24
**Trigger:** g55 codex session `b07da0af-da8b-4024-91d4-aca7ae82deaf`, the first
real-world attempt by an agent to recover prior work from Longhouse history under time
pressure.
**Reviewed by:** `hatch codex sol`, `hatch openrouter kimi-k3`. Both reviews were
valuable and both reached a central recommendation this document now rejects; see §8.

---

## 1. What happened

David was in the car with minutes to spare. He asked a managed Codex session to push a
new Android build to a tablet on the same LAN. The agent treated the tablet as
unprovisioned, asked him to plug in USB, and offered manual steps. David pushed back
twice: *"They're on the same network. You have to do this for me."* and *"you've
completely missed an entire set of work. It's on debugging. I've done all this. Check
the notes."*

The agent then did the right thing — it went looking for the prior session that had
already paired the tablet for wireless ADB. What followed:

1. `command -v recall` → `command not found`.
2. `rg` over the Obsidian journal → zsh glob failure, no matches.
3. `longhouse.peers` → **worked**, returned the sibling g55 session with live control facts.
4. `longhouse.tail(limit=120)` → **422**, `limit` must be ≤ 100.
5. `longhouse.tail(limit=100)` → worked, but returned `"Script completed / Wall time 0.1
   seconds"` tool spam. Called twice, byte-identical output both times.
6. `lifehub-evidence search --source longhouse_mirror` → **fifteen queries**, almost all
   `count: 0`, including five that passed `--since`/`--until` filters that were silently
   discarded.
7. David: *"Don't waste too much time on this."* Then: *"Just tell me."*

The agent never found the answer. It gave up on retrieval and asked David to plug in USB.

### The counterfactual

Verified against hosted `david010` while writing this document:

```
GET /api/agents/sessions?query=adb&project=g55&days_back=14
→ b07da0af (7/24), 97a30378 (7/21), 8bcd19ea (7/20), 6bd9fff7 (7/20)
```

One lexical call returns four candidate sessions, including the exact July 21 session
the agent eventually guessed at by hand. The REST endpoint exists and works. No tool
reachable from that session could call it.

---

## 2. Root cause

### 2.1 The managed tool surface is a hardcoded five-tool list in Rust (severity: critical)

`engine/src/claude_channel_server.rs:278-306`, `coordination_tools()`, advertises exactly
`peers`, `tail`, `send`, `inbox`, `reply`. There is no history-search tool and no env
check — it is not a gate, it is an unfinished surface.

**All three managed providers reach this same Rust facade.** Verified:

| Provider | Registration | Source |
|---|---|---|
| Claude | `longhouse-engine claude-channel serve` ×2 (channel + coordination) | `server/zerg/cli/claude.py:186-205` |
| Codex | writes `~/.codex/config.toml` → `longhouse-engine claude-channel serve` | `engine/src/longhouse.rs:1280-1290` |
| OpenCode | `longhouse-engine claude-channel serve` | `engine/src/opencode_bridge.rs:178-196` |

Confirmed against David's live config:

```toml
[mcp_servers.longhouse]
command = "/Users/davidrose/.local/bin/longhouse-engine"
args = ["claude-channel", "serve"]
```

The product invariant in `docs/specs/agent-session-recall-continuity.md:64` is to
"import or start sessions, find them fast, and steer them." Steer shipped on this
surface. **Find was never built on it.**

### 2.2 The Python MCP server has the search tools and cannot be reached (severity: critical)

`server/zerg/mcp_server/server.py` implements nine tools including `search_sessions`,
`recall`, and `get_session_detail`. It is invoked as `longhouse-server mcp-server`
(hidden subcommand, `cli/main.py:717`).

`longhouse-server` is the **Runtime Host** Python service. Per
`docs/specs/native-device-runtime.md:1-11`:

> The installed Longhouse device product is two paired Rust binaries... The Runtime Host
> is a separate Python service named `longhouse-server`. It runs where the web
> application and SQLite database live. **The device installer never installs Python,
> `uv`, or a server command.**

The hermetic installer smoke actively traps `python`, `python3`, `uv`, and `pip` to prove
this. So the search tools live in a component that by design does not exist where agents
run. Verified on this machine:

```
$ longhouse mcp-server --help
error: unrecognized subcommand 'mcp-server'
```

There is a corollary defect. `~/git/me/registry/mcp-registry.toml:17-20` declares:

```toml
[servers.longhouse]
command = "longhouse"
args = ["mcp-server"]
```

That is **broken config for every agent `agents sync` touches.** The shipped `longhouse`
is the Rust device binary and has no `mcp-server` subcommand. For Codex it is
additionally overwritten at launch by `configure_codex_coordination_mcp()`. The Python
MCP server is, in practice, unreachable dead code from any device agent.

### 2.3 The env-keyed strip in the Python server is a real bug, and not this one

`server/zerg/mcp_server/server.py:577-580`:

```python
if str(os.environ.get("LONGHOUSE_COORDINATION_TOKEN") or "").strip():
    for tool_name in tuple(server._tool_manager._tools):
        if tool_name not in _COORDINATION_TOOL_NAMES:
            server.remove_tool(tool_name)
```

This strips `search_sessions`, `recall`, `get_session_detail`, **and**
`notify_longhouse` when coordination authority is present. It adds no authority: the
write tools already enforce the token at call time (`server.py:468-470`, `517-519`,
`557-559`), and archive reads authenticate with the device token that
`mcp_serve.py:43-44` loads from stored config regardless of coordination env.

It is also a process-global footgun: under the streamable-HTTP transport, one connection
with that env set removes search for every connected client.

But it did not cause this incident, because no managed provider reaches this server. Both
external reviews identified this strip as the root cause and proposed deleting it as the
90/10 fix. **That change would not have altered the g55 session at all.** Fix it because
it is wrong, not because it is load-bearing.

### 2.4 `tail` is unclamped in Rust and unbounded in its advertised schema (severity: medium)

Server: `agents_sessions.py:964` → `Query(30, ge=1, le=100)`. Python MCP clamps with
`min(limit, 100)` (`server.py:367`). The Rust facade passes the value verbatim and its
advertised `inputSchema` declares no `maximum`, so an agent cannot know the bound before
it fails. One retry recovers, which is why this is medium and not high.

### 2.5 `tail` is unreadable on tool-heavy sessions and cannot page (severity: high)

On the g55 session — 466 tool calls, 18 user messages — a default `tail` is ~96%
`"Script completed / Wall time 0.1 seconds"`. There is no role filter, so there is no way
to ask for the turns where decisions live.

Worse, `tail` takes only `session_id` and `limit` (`agents_sessions.py:961-964`). There is
**no pagination**. Content earlier than the last 100 events is structurally unreachable
through this tool. Discovery without readable, reachable inspection does not close the
loop — the agent *had* `tail`, used it, and learned nothing.

### 2.6 `peers` hides ended sessions by default (severity: high)

`peers` defaults `active_only=True` and filters client-side on `has_live_presence`
(`server.py:380`, `425`), over a 7-day wall window. The July 21 session had ended. It was
invisible to the default call, the tool describes itself as reading "the live Longhouse
wall," and nothing in `COORDINATION_INSTRUCTIONS` teaches an agent to pass
`active_only=false`.

`peers` is a liveness tool. It was the only discovery tool the agent had, and history is
not what it does.

### 2.7 The Life Hub mirror matcher is a whole-query substring match (severity: high)

`life-hub/src/life_hub/db/operations/evidence.py:1412-1414`:

```python
pattern = f"%{_escape_like_pattern(query.strip())}%"
filters.append(f"c.chunk_text ILIKE {_bind(pattern)} ESCAPE '\\'")
```

The entire query becomes one contiguous `ILIKE` pattern. Measured live:

| query | hits |
|---|---|
| `adb` | 1 |
| `wireless debugging` | 1 |
| `debugging wireless` | **0** |
| `adb tablet` | **0** |

Word order matters, confirming contiguous substring rather than term intersection. Any
multi-word natural-language query returns zero unless the phrase appears verbatim.
`count: 0` reads as *"not in the corpus"* rather than *"wrong query shape"* — the direct
driver of the fifteen-query flail. The agent was getting a shape error and read it as
absence, so it rephrased instead of shortening.

### 2.8 The mirror CLI silently drops filters (severity: medium)

`~/git/me/scripts/lifehub-evidence.py:540-546` sends only `q` and `limit` on the mirror
path, while the Life Hub API (`api/routers/evidence.py:121-143`) accepts `provider`,
`project`, `device`, `sidechain`, `since`, and `until` and applies all of them as SQL
filters.

- `--since` / `--until` are **defined on the search subparser and silently discarded**.
  The agent ran five queries believing it had narrowed to a two-day window.
- `--provider` / `--project` / `--device` are **not defined on the CLI**, though the API
  supports them and the response echoes them as `null` — which reads as "supported, unset."

Reading a mirror hit is possible but awkward: an `agent-chunk` subcommand exists
(`lifehub-evidence.py:1285-1294`) requiring `--session-id`, `--transcript-revision`, and
`--chunk-index`. There is no `read --source longhouse_mirror` verb.

### 2.9 Documentation and tools that point at nothing (severity: medium)

- `notify_longhouse` advertises "send a notification to the Longhouse coordinator" and
  its body is `logger.info(...)` returning `delivered: false`.
- `get_session_detail` (`server.py:201`) and `recall` (`server.py:299`) docstrings route
  agents to `query_agents` for exact match. **No such tool exists.**
- `recall`'s failure hint says recall "needs the derived search index"
  (`server.py:330-335`). In catalog mode `/api/agents/recall` serves from storage-v2
  search and `/api/agents/recall/status` returns `{"status":"retired"}`. The hint is stale.
- The global `AGENTS.md` bullet reads "use Longhouse for agent-log-only/session/transcript
  search (`search_sessions` / `recall`)". Backticked tool names in prose invited
  `command -v recall`. A `longhouse-server recall` CLI command does exist, which makes the
  ambiguity worse rather than better.

---

## 3. What worked

- **`peers` returned real control facts** (`kernel_control_label: live`,
  `kernel_observe_only: false`), not just a session list.
- **`tail` returned real cross-session transcript content** from a three-day-old session.
- **The canonical corpus had the answer**, and canonical lexical discovery found it in one
  call with no tuning.
- **Life Hub's provenance discipline held.** Every mirror response stamped
  `source_role: mirror`, `canonical_source: longhouse`, plus a stderr warning naming
  `search_sessions`/`recall` as canonical. The system told the agent it was on the wrong
  surface.
- **The agent's instinct was right.** Told "check the notes," it searched durable history
  rather than asking David to redo setup. That is the behavior the product exists to
  enable.

### 3.1 The counter-argument, and why it fails

Steelmanned: *the agent had `peers` and `tail`, both worked, and `peers` exposes
`active_only`. It should have called `peers(active_only=false)`, walked the 7-day wall,
and tailed each g55 candidate. Five calls, no search tool needed. Instead it burned
fifteen mirror queries while ignoring a stderr warning that named the canonical surface.*

The agent-error half is real and this document should not excuse it: it ignored an
explicit pointer, never widened `peers`, and confused zero hits with absence.

But it cannot be the root cause, because three structural facts make even optimal play a
luck-dependent path:

1. `peers` defaults to live-only over 7 days (§2.6); the target session was ended.
2. `tail` cannot page (§2.5); the answer must happen to be in the last 100 events of a
   correctly-guessed session.
3. Nothing in the surface searches *within* sessions, so "walk the peers" is
   O(sessions × noise) with a hard ceiling.

Agent error was a contributing cause. The absent tool was the root cause.

---

## 4. How Longhouse and Life Hub relate

```
Longhouse (canonical)                    Life Hub (mirror)
─────────────────────                    ─────────────────
owns raw agent history          ──────>  agent_sessions source
SQLite, hosted david010                  Postgres, 266k chunks, ~174s lag
/api/agents/{sessions,recall,tail}       /api/evidence/agent-sessions/{search,chunk}
                                         one of nine mirrored sources
```

The global `AGENTS.md` routing bullet makes Longhouse canonical for agent-log search and
Life Hub's feed a mirror. **The layering is correct and the labeling is honest.** The
defect is asymmetric reachability: the mirror is reachable from a managed session and
canonical search is not, so the documented-inferior fallback is the only callable one.

The mirror keeps a legitimate audience after this work: non-MCP shells — hatch workers,
plain terminals — where no Longhouse MCP is configured. For those it is the only
reachable surface. That is why its honesty defects are worth fixing even though its
ranking and matcher are not. Invest in honesty, not capability.

---

## 5. Design

### 5.1 Principles

1. **The managed surface must serve the product's core verbs.** Find is a core verb.
2. **Native device surface or not offered** (`native-device-runtime.md:20`). Device-side
   tools are Rust. Python is Runtime Host only.
3. **Errors teach the next call.** Every recoverable failure states how to recover.
4. **Delete what lies.** A tool that advertises what it does not do costs a call and a
   wrong mental model.

### 5.2 Add `search_sessions` to the Rust facade

The fix goes where the surface actually is: `coordination_tools()` and
`call_coordination_tool()` in `engine/src/claude_channel_server.rs`. It proxies
`GET /api/agents/sessions?query=…` exactly as `peers` already proxies
`/api/agents/sessions/wall` — about 40 lines, one new dispatch arm.

Scope: `search_sessions` only. The demonstrated recovery journey is `search_sessions` →
`tail(roles="user,assistant")`, two calls. `recall` is slower and carries storage-v2
naming ambiguity; `get_session_detail` overlaps `tail` at higher cost. Three new schemas
in every managed session's namespace is not justified by a two-call journey.

### 5.3 Rejected: route managed providers at the Python MCP server

Both reviews recommended pointing Claude and OpenCode at `longhouse mcp-server` and
deleting the Rust facade as a duplicate. This is rejected on evidence neither review had:

- **It violates the device contract.** `native-device-runtime.md` states the device
  installer never installs Python or a server command, and the installer smoke traps
  `python`/`uv`/`pip` to enforce it.
- **`longhouse mcp-server` does not exist.** The shipped `longhouse` is Rust.
- **It is a known regression.** Commit `92004107a` (2026-07-23) moved these tools *into*
  Rust precisely because the Python path broke when `4b3f2da60` renamed the entrypoint to
  `longhouse-server`. Reverting to it reintroduces a fixed bug.
- **Codex does not use the Python server either.** `configure_codex_coordination_mcp()`
  overwrites `~/.codex/config.toml` to point at the engine, so the premise that "Codex
  already does this" — which both reviews relied on — is false.

The Rust facade is not a duplicate to delete. It is the native implementation. The Python
MCP server is the redundant one; it is unreachable from devices and should either be
deleted or explicitly scoped to Runtime Host use.

### 5.4 Errors that teach

- **Bound violation** → declare `minimum: 1` / `maximum: 100` in the advertised schema and
  clamp instead of 422.
- **Zero hits on a substring matcher** → report `match_mode: "exact_substring"` and hint
  to shorten the query. Not per-term diagnostics: the matcher has no per-term semantics,
  so reporting them would be a second lie.
- **Unsupported filter** → forward it or reject it loudly. Never accept and discard.

---

## 6. What shipped

`cb4be121a`, `d9441d7eb`, `3244f3608` (longhouse) and `d4c020b`, `1128f04` (me).

**Managed surface — `engine/src/claude_channel_server.rs`.** `search_sessions` added to
`coordination_tools()` and `call_coordination_tool()`, proxying
`GET /api/agents/sessions` with the device token that `peers`/`tail` already use. `limit`
clamped to `1..=100` and `days_back` to `1..=90`, both advertised, because the archive
422s past either. Empty query rejected rather than forwarded, since the archive matches it
literally and returns zero — which reads as absence. `search_sessions` added to
`LONGHOUSE_COORDINATION_TOOLS` in `codex_bridge.rs` so Codex auto-approves it. The
`initialize` instructions now say to search history before asking a user to redo work, and
that `peers` is live-only unless `active_only=false`.

**`tail` readability — `server/zerg/routers/agents_sessions.py`.** A `roles` filter applied
before the limit, so `roles=user,assistant` returns that many real turns instead of that
many mostly-tool events. Storage-v2 has no role predicate, so a narrowed request scans a
bounded wider window (`limit * 25`, capped 1000) and trims after filtering; the legacy path
filters in SQL. The response reports `scan_window` (events actually examined, `None` when
the store filtered across the whole session) and `window_exhausted`, which is the answer
`tail` previously could not give: a short result was ambiguous between "no more turns" and
"the scan ran out", and with bounded scanning that distinction is real.
`longhouse-server tail --roles` gets the same filter and surfaces the warning.

**Runtime Host MCP server — `server/zerg/mcp_server/server.py`.** A separate surface that
devices do not load (§2.2), fixed because it was wrong, not because it was load-bearing.
The coordination-token tool strip is gone; authority is enforced per call in
`send`/`inbox`/`reply`, so the strip protected nothing and was process-global under the
HTTP transport. `notify_longhouse` removed — it logged and returned `delivered: false`.
`query_agents` docstring references removed; no such tool exists.

**Life Hub mirror — `~/git/me/scripts/lifehub-evidence.py`.** `since`/`until` are now
forwarded instead of parsed and discarded; `--provider`/`--project`/`--device` added and
forwarded. Responses carry `match_mode: "exact_substring"`, and a multi-token zero-hit
explains that the matcher is whole-query substring and names the canonical surface.

**Registry — `~/git/me/registry/mcp-registry.toml`.** Both Longhouse entries were dead
config: `longhouse` has neither an `mcp-server` nor a `claude-channel` subcommand, so
unmanaged agents had no Longhouse tools at all. `servers.longhouse` now points at
`longhouse-server mcp-server`, which is the surface that actually serves history tools off
a Runtime Host. The `longhouse-channel` entry is removed; managed launches inject the
native channel themselves and unmanaged sessions cannot use channel mode. The untracked
legacy `mcp/servers.json` was corrected locally so nobody can regenerate the broken
commands from it.

**Guidance — global `AGENTS.md`.** `search_sessions`/`recall` named as MCP tools rather
than backticked in prose beside shell commands, which is what sent an agent looking for a
`recall` binary; `longhouse-server recall` named as the CLI equivalent; the mirror's
substring semantics stated so a zero result is not read as absence.

### Verification

`make test`: 3649 passed, 1 pre-existing failure
(`test_managed_provider_contracts` generated-manifest digest, confirmed failing on a
stashed clean tree and last touched by unrelated commits). `make test-engine`: clean.
Life Hub: 40 passed. Both ships green — runs 30140127523 and 30140928388, demo and canary
healthy on the exact SHAs.

The journey the g55 agent could not make, run against hosted `david010` on the shipped code:

```
search_sessions(query="adb", project="g55")
  → b07da0af, 97a30378, 8bcd19ea, 6bd9fff7
tail(97a30378, roles="user,assistant", limit=8)
  → 8 real turns, scan_window 200, window_exhausted false
```

Two calls. The second returns decision prose; before this change the same call returned
eight lines of `Script completed / Wall time 0.1 seconds`.

### Deliberately not done

- **`tail` pagination.** Storage-v2 role filtering is bounded at 1000 events, so a matching
  turn older than that is still unreachable. `window_exhausted` makes that honest rather
  than fixing it; the fix needs a cursor and its own design.
- **Mirror hits do not carry a "read via Longhouse" pointer.** Only zero-hits name the
  canonical surface. Packets already include `session_id`, and `agent-chunk` covers
  coordinate reads, so this was not worth another field.
- **`--strict-mcp-config` for managed Claude.** Would stop the duplicate Python/native
  Longhouse surfaces, but would also strip docket-hub, image-hub and the rest from managed
  sessions. The registry comment now states the duplication instead.
- **Deleting or rescoping the Python MCP server.** It is unreachable from a device (§2.2)
  and now reachable for unmanaged agents via the fixed registry entry. Whether it should
  exist at all is a real question, left open.

### Out of scope

- Semantic/embedding recall quality. `recall` scored 0.018 on a well-formed query, which is
  poor, but canonical lexical search answered the question in one call.
- Mirror matcher redesign (FTS, term intersection, ranking). Honesty about current
  semantics is in scope; changing them is not.
- `tail` pagination. Real gap (§2.5), larger than this incident; needs its own design.
- Deleting or rescoping the Python MCP server. Called out in §5.3, deliberately not
  actioned here.

---

## 7. The lesson

The corpus had the answer. The REST endpoint that finds it worked on the first call.
Provenance labeling was honest. Every retrieval component that exists, works.

An agent under time pressure, correctly instructed to check durable history, could not
reach any of it — because the tool surface every managed provider actually loads was built
for steering and never got a find verb, while the component that *has* one lives in a
service the device contract forbids installing on a device.

## 8. Note on the review process

Both reviews were rigorous, verified real code, and caught real errors in the first draft
— Sol corrected the authority model and the mirror's matcher semantics; kimi supplied the
`peers active_only` and `tail` pagination gaps that make §3.1's rebuttal work, and correctly
demoted several severities.

Both also converged on a central recommendation — delete the Rust facade, route managed
providers at the Python MCP server — that is wrong, and wrong in an instructive way. Each
read the code accurately and inferred that because `codex_bridge.rs` injects env for a
`longhouse` MCP server, Codex must be running the Python one. Neither checked
`~/.codex/config.toml` or `engine/src/longhouse.rs:1280`, where the launcher overwrites
that entry to point at the engine. The first draft of this document made the same
inference, so the reviews confirmed an error rather than catching it.

What broke the tie was not a better argument. It was a durable project note recording that
this exact change had been made before and had broken, plus four commands against the live
system: `longhouse mcp-server --help`, `grep ~/.codex/config.toml`, `git show 4b3f2da60`,
and reading `native-device-runtime.md`. Agreement among reviewers reading the same code is
not independent confirmation. Checking what the machine is actually running is.
