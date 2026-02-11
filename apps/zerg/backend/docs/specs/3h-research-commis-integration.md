# Phase 3h Research: Commis Integration Alternatives

**Status:** Complete (research)
**Date:** 2026-02-10
**Author:** Agent (Phase 3h research task)

## Executive Summary

Longhouse currently spawns commis (sub-agents) via `hatch`, a thin Python CLI that wraps Claude Code, Codex, and Gemini CLIs as headless subprocesses. Two newer integration paths exist: the **Codex App Server protocol** (JSON-RPC over stdio with Thread/Turn/Item primitives) and the **Claude Agent SDK** (`@anthropic-ai/claude-agent-sdk`, TypeScript). Both offer structured real-time streaming and programmatic lifecycle control that the current subprocess approach lacks. However, adopting either would mean maintaining provider-specific integration code and adding a Node.js runtime dependency. The recommendation is to **stay with the hatch subprocess approach for now**, but add a lightweight event streaming layer using Claude's `--output-format stream-json` and Codex's App Server as optional "rich mode" backends, activated per-provider rather than replacing the unified hatch path.

---

## Comparison Table

| Dimension | Hatch Subprocess (Current) | Codex App Server Protocol | Claude Agent SDK (TypeScript) |
|---|---|---|---|
| **Transport** | stdio (stdin prompt, capture stdout/stderr) | JSON-RPC over stdio (long-lived process) | TypeScript async generator (in-process) |
| **Streaming** | Batch only (output after completion); `stream-json` partially supported | Real-time: `item/*/delta`, `turn/*`, `item/completed` notifications | Real-time: `SDKMessage` async generator with `stream_event` deltas |
| **Lifecycle Control** | Kill process group (SIGKILL); timeout only | `turn/start`, `turn/cancel`; approval pause/resume | `AbortController`, `interrupt()`, `setModel()`, `setPermissionMode()` mid-run |
| **Tool Injection** | MCP server config in workspace settings (3f) | Not directly; Codex uses its own tool system | `tool()` + `createSdkMcpServer()` for in-process MCP tools |
| **Approval Routing** | `--dangerously-skip-permissions` / `--full-auto` | Structured: `requestApproval` events with accept/decline response | `setPermissionMode()` per query; no structured approval events |
| **Multi-Provider** | 4 backends (zai, bedrock, codex, gemini) via single CLI | Codex only | Claude only (any Anthropic-compatible endpoint) |
| **Language** | Python (hatch) + any CLI | Any language (JSON-RPC stdio client) | TypeScript/Node.js only |
| **Session Persistence** | JSONL files on disk (provider-native) | Thread objects (durable, resumable) | Session-based (V2 preview: `createSession`/`resumeSession`) |
| **Schema Generation** | N/A | `codex app-server generate-json-schema` | TypeScript types from npm package |
| **Maturity** | Stable, battle-tested | Public but evolving (protocol drift noted by OpenAI) | v0.2.39 (Feb 2026); V2 preview available |
| **Integration Effort** | Already done | Medium: JSON-RPC client + event routing (~500 LOC) | Medium-High: Node.js sidecar or Bun service (~800 LOC) |
| **OSS User Impact** | None (hatch is `uv tool install`) | Requires Codex CLI installed | Requires Node.js + npm package |

---

## Detailed Analysis

### Option 1: Hatch Subprocess (Current Approach)

**How it works:** `CloudExecutor` in `cloud_executor.py` spawns `hatch -b <backend> --model <model> -C <workspace> "<prompt>"` as an async subprocess. Hatch normalizes CLI differences across Claude Code (`claude --print`), Codex (`codex exec`), and Gemini (`gemini -p`). Output is captured after process completion. Session JSONL is ingested into the timeline post-hoc via `AgentsStore.ingest_session()`.

**Strengths:**
- **Unified multi-provider interface.** One code path handles 4 backends. Adding a new CLI agent means adding a `configure_*()` function in hatch (~40 LOC).
- **Process isolation.** Each commis runs in its own process group with timeout enforcement and SIGKILL cleanup. No shared state, no memory leaks.
- **No runtime dependency beyond the CLI.** OSS users install `hatch` via `uv tool install` and whichever agent CLI they want.
- **Already integrated.** Workspace management, MCP server injection (3f), quality gate hooks (3g), timeline ingestion, and SSE events all work with this approach today.
- **Partial streaming already possible.** `--output-format stream-json` for Claude backends and `--include-partial-messages` are supported but not yet wired to real-time UI.

**Weaknesses:**
- **No structured event stream.** Output is captured as a blob after completion. Real-time progress requires polling or parsing incremental stdout, which is fragile.
- **Coarse lifecycle control.** The only controls are "start" and "kill." No pause, resume, model switch, or permission change mid-run.
- **Post-hoc timeline ingestion.** Session data is only available after the subprocess exits. There is a delay between commis work and timeline visibility.
- **Approval routing is all-or-nothing.** `--dangerously-skip-permissions` and `--full-auto` skip all approvals. There is no way for Longhouse to selectively approve/deny tool use.

**Verdict:** Good enough for the current product stage. The weaknesses become significant only when Longhouse needs real-time commis observability in the UI (showing live tool calls, streaming output, interactive approval).

---

### Option 2: Codex App Server Protocol

**How it works:** Instead of `codex exec`, you run `codex app-server` as a long-lived child process. Communication is JSONL over stdio using a JSON-RPC-like protocol (no `"jsonrpc": "2.0"` header). The client sends requests (`initialize`, `thread/start`, `turn/start`) and receives notifications (`item/started`, `item/agentMessage/delta`, `item/commandExecution/outputDelta`, `item/completed`, `turn/completed`).

**Key primitives:**
- **Thread** = durable conversation container (maps to a commis session)
- **Turn** = one unit of agent work from a user message (maps to a commis task)
- **Item** = atomic event (message, tool call, file change, approval request)

**Protocol flow:**
1. Spawn `codex app-server` as child process
2. Send `initialize` request, wait for response, send `initialized` notification
3. `thread/start` or `thread/resume` to create/resume a conversation
4. `turn/start` with prompt to kick off work
5. Stream notifications: `item/started` -> `item/*/delta` -> `item/completed` -> `turn/completed`

**Approval routing:** The protocol supports structured approval. When Codex wants to run a command or change a file, it emits `item/commandExecution/requestApproval` or `item/fileChange/requestApproval`. The client responds with `{ "decision": "accept" | "decline" }`. This enables Longhouse to implement selective tool gating.

**Schema generation:** Run `codex app-server generate-json-schema --out ./schemas` to get the full type definitions for the protocol, reducing protocol drift risk.

**Strengths:**
- **Structured real-time streaming.** Every tool call, message delta, and file change arrives as a typed event. No stdout parsing.
- **Approval routing.** Longhouse could intercept dangerous operations and surface them to the user for approval, rather than blanket auto-approve.
- **Durable threads.** Thread resume enables multi-turn commis work without losing context.
- **Schema-first.** Auto-generated schemas reduce integration maintenance.

**Weaknesses:**
- **Codex only.** This protocol is specific to the Codex CLI. Claude Code and Gemini have no equivalent server mode.
- **Protocol instability.** OpenAI describes earlier versions as "unofficial" and the protocol is still evolving. Breaking changes are possible.
- **Long-lived process management.** Instead of fire-and-forget subprocesses, Longhouse would need to manage persistent Codex server processes, handle reconnection, and deal with server crashes.
- **Python JSON-RPC client needed.** Would need to write or adopt a JSON-RPC stdio client in Python (~300-500 LOC for the protocol layer, plus event routing).
- **No multi-provider benefit.** The investment only improves Codex-backend commis. Claude and Gemini still need the subprocess path.

**Verdict:** Valuable for real-time Codex commis observability and approval routing. Not worth building as the primary integration path because it only covers one provider. Best adopted as an optional "rich mode" for Codex commis alongside the existing hatch path.

---

### Option 3: Claude Agent SDK (TypeScript)

**How it works:** The `@anthropic-ai/claude-agent-sdk` npm package (v0.2.39, released 2026-02-10) provides a TypeScript API that wraps Claude Code's execution engine. The `query()` function returns an async generator of `SDKMessage` events including assistant messages, tool use, and stream deltas. Custom tools are injected via MCP servers (in-process or remote).

**Key API:**
```typescript
import { query, tool, createSdkMcpServer } from '@anthropic-ai/claude-agent-sdk'

const q = query({
  prompt: 'Fix the bug in auth.py',
  options: {
    model: 'claude-opus-4-6',
    mcpServers: { longhouse: longhouseMcpServer },
    allowedTools: ['search_sessions', 'memory_read'],
    abortController: new AbortController(),
    includePartialMessages: true,
  },
})

for await (const msg of q) {
  // msg.type: 'assistant' | 'stream_event' | 'result' | ...
}
```

**Lifecycle control:** The `Query` object supports `interrupt()`, `setModel()`, `setPermissionMode()`, and `setMaxThinkingTokens()` mid-run. An `AbortController` enables hard cancellation.

**V2 Preview:** A session-based API (`createSession`/`resumeSession`/`session.send`/`session.stream`/`session.close`) provides explicit session lifecycle management, which maps well to Longhouse's commis session model.

**Strengths:**
- **Richest lifecycle control.** Pause, interrupt, change model, change permissions — all programmatically, mid-run.
- **Native tool injection.** Define tools in TypeScript with Zod schemas and serve them as an in-process MCP server. No external process needed.
- **Real-time streaming.** Token-level deltas via `stream_event` messages when `includePartialMessages` is true.
- **Session persistence.** V2 preview supports durable sessions with explicit create/resume/close.
- **Official SDK.** Maintained by Anthropic, versioned on npm, TypeScript types included.

**Weaknesses:**
- **Claude only.** No Codex, no Gemini, no z.ai support. Longhouse would need to maintain the hatch path for non-Claude backends regardless.
- **TypeScript/Node.js dependency.** Longhouse's backend is Python (FastAPI). Integrating the SDK requires either: (a) a Node.js/Bun sidecar service that the Python backend communicates with, or (b) rewriting commis execution in TypeScript. Both add significant operational complexity.
- **Bun compatibility unknown.** While Bun is the project's JS runtime of choice, SDK compatibility with Bun (vs Node.js) is not guaranteed.
- **API instability.** V1 `query()` and V2 `session` APIs coexist. The V2 is explicitly labeled "preview." Migration cost if V1 is deprecated.
- **Billing implications.** The SDK calls the Anthropic API directly. Using z.ai (flat-rate) requires the `ANTHROPIC_BASE_URL` override to work with the SDK, which is unverified. Bedrock integration via the SDK is also unverified.
- **OSS user impact.** Self-hosters would need Node.js installed in addition to Python and the agent CLIs.

**Verdict:** The richest integration for Claude-backend commis, but the language boundary (Python backend <-> TypeScript SDK) makes it impractical as the primary execution path. Better suited for a future world where Longhouse has a TypeScript execution layer, or if commis execution is moved to a Bun-based sidecar.

---

## Architecture Implications

### What Real-Time Streaming Actually Requires

The core value of both the Codex App Server and Claude Agent SDK is **real-time event streaming** — showing users what a commis is doing as it works, not just the result after it finishes. To deliver this value, Longhouse would need:

1. **Event routing layer:** Parse provider-specific events into a common `CommisEvent` schema
2. **SSE/WebSocket push:** Stream events to the frontend in real time
3. **Timeline incremental ingest:** Write events to the timeline as they arrive, not post-hoc
4. **UI components:** Live commis activity view with tool calls, file changes, messages

This is a significant product investment regardless of which integration path is chosen. The protocol/SDK choice is downstream of the decision to build real-time commis observability.

### Multi-Provider Reality

Longhouse supports 4 backends today. Any integration path that only covers one provider creates a maintenance asymmetry:

| Backend | Hatch Subprocess | Rich Mode (if built) |
|---------|-----------------|---------------------|
| Claude (zai/bedrock) | `claude --print --output-format stream-json` | Claude Agent SDK or stream-json parsing |
| Codex | `codex exec` | Codex App Server protocol |
| Gemini | `gemini -p` | No equivalent (subprocess only) |

Building rich integrations for Claude AND Codex while keeping Gemini on subprocess means three code paths for commis execution. This is maintainable but increases surface area.

---

## Recommendation

### Short Term (Now): Stay with Hatch, Add Stream-JSON Parsing

1. **Keep hatch subprocess as the primary execution path.** It works, it is multi-provider, and it is already integrated with workspace management, MCP injection, quality gates, and timeline ingestion.

2. **Wire `--output-format stream-json` for Claude backends.** This is already supported in hatch and `CloudExecutor` but not connected to real-time UI. Parse the JSONL stream incrementally instead of waiting for process exit. This gives ~80% of the Claude Agent SDK's streaming value with ~10% of the effort.

3. **Add incremental stdout parsing in CloudExecutor.** Instead of `proc.communicate()`, read stdout line-by-line and emit `CommisEvent` SSE events as they arrive. This works for any backend that produces JSONL output.

### Medium Term (When Real-Time Commis UI Is Prioritized): Add Codex App Server

4. **Adopt Codex App Server as an optional rich mode for Codex-backend commis.** When a commis uses the Codex backend, spawn `codex app-server` instead of `codex exec`. Route the Thread/Turn/Item events through the same `CommisEvent` SSE path. This unlocks approval routing and structured events for Codex.

5. **Do NOT adopt Claude Agent SDK until the language boundary is resolved.** The Python-to-TypeScript bridge (sidecar or FFI) is not worth the complexity today. If Longhouse ever gets a Bun-based execution layer (e.g., for the runner daemon), revisit then.

### Long Term (If Longhouse Needs Deep Agent Control): Evaluate SDK-Based Execution

6. **If Longhouse needs mid-run model switching, selective approval, or programmatic tool injection beyond MCP**, the Claude Agent SDK and Codex App Server become the right tools. At that point, consider a Bun sidecar service that handles commis execution for Claude and Codex, with the Python backend dispatching to it via HTTP/WebSocket.

### Priority of Investment

| Priority | Action | Effort | Value |
|----------|--------|--------|-------|
| 1 | Wire stream-json parsing in CloudExecutor for Claude backends | Small (1-2 days) | Real-time Claude commis events |
| 2 | Add incremental stdout parsing for all backends | Small (1 day) | Partial streaming for Codex/Gemini |
| 3 | Codex App Server integration (optional rich mode) | Medium (3-5 days) | Structured Codex events + approval |
| 4 | Claude Agent SDK via Bun sidecar | Large (1-2 weeks) | Full Claude lifecycle control |

---

## References

- Codex App Server docs: https://developers.openai.com/codex/app-server
- Codex Harness blog post: https://openai.com/index/unlocking-the-codex-harness/
- Claude Agent SDK (TypeScript): https://platform.claude.com/docs/en/agent-sdk/typescript
- Claude Agent SDK V2 Preview: https://platform.claude.com/docs/en/agent-sdk/typescript-v2-preview
- Claude Agent SDK npm: `@anthropic-ai/claude-agent-sdk` (v0.2.39)
- Claude Agent SDK migration guide: https://platform.claude.com/docs/en/agent-sdk/migration-guide
- Hatch source: `~/git/hatch/src/hatch/` (backends.py, runner.py, cli.py)
- CloudExecutor: `apps/zerg/backend/zerg/services/cloud_executor.py`
- Harness Simplification spec: `apps/zerg/backend/docs/specs/unified-memory-bridge.md`
