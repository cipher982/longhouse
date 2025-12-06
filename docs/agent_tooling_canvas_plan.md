# Agent Tools & Canvas Workflow – Master Design Doc

_Status: 🟡 IN PROGRESS — Phase A Complete, Phases B-D Active_
_Last Updated: 2025-06-15 — adopted as the **source-of-truth** for the upcoming “Tooling & Canvas” epic._

---

## 1 Purpose

This document aggregates the architectural context, design principles and phased rollout plan we agreed on while discussing how to evolve:

- the **backend** (true agent ReAct loop, tool runtime, metrics), and
- the **frontend** canvas UX (nodes, hierarchy, live debug).

It is intentionally _code-agnostic_: no function names or API signatures are frozen here. Implementation specifics live in PRs and inline code comments, but **goals, principles and scope live here and must be kept up-to-date**.

---

## 2 Context Snapshot (May 2025)

Backend
• Agents already run a ReAct loop (`LLM → Tool → LLM … until done`).
• Only one demo tool (`get_current_time`) exists; no shared registry.
• No timeouts, retries, per-tool metrics or permission model.

Frontend
• Canvas supports AgentIdentity nodes only; no Tool/Trigger/Condition nodes yet.
• Dashboard & chat UI already stream tool output via WebSockets.

Business goal

> Enable non-technical users to visually build **event-driven workflows** that combine AI reasoning (Agents) with deterministic operations (Tools) in a single canvas.

---

## 3 First-Principles Foundations

1. **What you draw is what runs.** Canvas objects map 1-to-1 to runtime units so there is no mental translation cost.
2. **One node, one responsibility.** An Agent node encapsulates its full internal loop. A Tool node performs exactly one deterministic action.
3. **Explicit data-flow.** Edges carry typed payloads; execution order follows edge direction.
4. **Progressive disclosure.** Beginners see collapsed nodes; power-users can drill down to inspect the agent’s internal LLM/Tool timeline.
5. **Live feedback by default.** Every running node surfaces state (spinner, ✓, ✗) and exposes inputs/outputs for debugging.

---

## 4 Concept Glossary

| Term               | Definition                                                                                                                                           |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Agent node**     | Black-box coroutine. Ingests a message/context, runs its internal while-loop (LLM ↔ Tools) until complete, then outputs the final assistant message. |
| **Tool node**      | Deterministic function executed _outside_ any agent. Useful for pre/post-processing or cost-efficient steps that don’t require reasoning.            |
| **Internal Tool**  | A Tool that lives _inside_ an Agent’s allow-list and can be invoked by the LLM during the loop.                                                      |
| **Trigger node**   | Entry point that fires a workflow (webhook, cron, manual).                                                                                           |
| **Condition node** | Branching logic on previous output.                                                                                                                  |
| **Nested canvas**  | Read-only or editable sub-graph that visualises an Agent’s internal steps.                                                                           |

---

## 5 UX Model

### 5.1 Top-level Canvas

```
┌──────────┐   ┌────────┐   ┌────────────┐   ┌─────────────┐
│ Webhook  │→ │ Agent A │→ │  Format     │→ │ Send Email  │
│ Trigger  │   │ (GPT-4) │   │  Tool      │   │  Tool       │
└──────────┘   └────────┘   └────────────┘   └─────────────┘
```

• Each node is a single block on the main canvas.
• Agent node animates its internal loop progress (e.g. _🧠1 → ⚙2 → 🧠3 → ✓_).

### 5.2 Drill-down / Inspect

Double-clicking an Agent opens a side-panel (or nested canvas) streaming every LLM and Tool step in real time. The user cannot re-wire internal calls here yet; it is a debug view.

---

## 6 Backend Architectural Guidelines

1. **ToolRegistry** Central registry in `zerg/tools/registry.py` with a `register_tool()` decorator. Supports discovery of built-ins and plugin entry-points.
2. **Allowed-tools list per Agent** New DB column (`allowed_tools` text[]). Empty list = “all tools”.
3. **Timeouts & retries** Every tool execution wrapped in `asyncio.wait_for`. Retry with exponential back-off (default 1 retry).
4. **Metrics** Prometheus counters & histograms: `tool_calls_total`, `tool_errors_total`, `tool_latency_seconds`.
5. **Execution semantics unchanged** `AgentRunner.run_thread()` still waits until the internal loop finishes before emitting the final assistant message to downstream workflow edges.

---

## 7 Phased Roll-out & Checklist

> Use the check-boxes to track progress. Update this section in each PR that advances an item.

### Phase A – Core refactor (target ≅ 2 weeks)

| ID  | Task                                                                      | Status |
| --- | ------------------------------------------------------------------------- | ------ |
| A-1 | Implement `ToolRegistry` & migrate `get_current_time`                     | [x]    |
| A-2 | Add built-in tools (`http_request`, `math_eval`, `uuid`, `datetime_diff`) | [x]    |
| A-3 | Schema migration – `allowed_tools` column                                 | [x]    |
| A-4 | Update agent factory to honour allow-list                                 | [x]    |

### Phase B – Reliability & Metrics (target ≅ 2 weeks)

| ID  | Task                                             | Status |
| --- | ------------------------------------------------ | ------ |
| B-1 | Timeout + retry wrapper around each Tool         | [x]    |
| B-2 | Emit Prometheus metrics per tool                 | [ ]    |
| B-3 | Persist per-tool timing & status in `runs` table | [x]    |

### Phase C – MCP Integration (target ≅ 2 weeks)

| ID  | Task                                               | Status |
| --- | -------------------------------------------------- | ------ |
| C-1 | MCP client adapter for tool registry               | [x]    |
| C-2 | Support for custom MCP server URLs                 | [x]    |
| C-3 | Preset popular MCP servers (GitHub, Linear, Slack) | [x]    |
| C-4 | OAuth token management for MCP servers             | [x]    |

### Phase D – Frontend Canvas (runs in parallel)

| ID  | Task                                         | Status |
| --- | -------------------------------------------- | ------ |
| D-1 | Render standalone Tool nodes with ⚙ icon     | [ ]    |
| D-2 | Agent node progress badge + click-to-inspect | [ ]    |
| D-3 | Debug side-panel streaming LLM/Tool timeline | [ ]    |
| D-4 | Basic port typing & edge validation          | [ ]    |

---

## 8 MCP Integration Strategy

### How MCP Fits the Canvas Vision

MCP (Model Context Protocol) provides a standardized way to connect to external tool providers, perfectly aligning with our plugin system needs:

1. **Built-in Tools** (Phase A) ✅ - Fast, reliable, always available
   - `get_current_time`, `math_eval`, `http_request`, etc.
   - Directly integrated into our tool registry

2. **MCP Tools** (Phase C) - Extensible ecosystem
   - **Preset Servers**: One-click setup for GitHub, Linear, Slack, etc.
   - **Custom Servers**: Customers can add ANY MCP server URL
   - Tools from MCP servers appear in the same registry as built-in tools

3. **Canvas Integration** (Phase D)
   - Tool nodes can represent either built-in OR MCP tools
   - Visual distinction shows tool source (built-in vs which MCP server)
   - Connection health indicators for external dependencies

### Customer Experience

```
Agent Configuration
├── Built-in Tools (always available)
│   ├── datetime tools
│   ├── math tools
│   └── http tools
├── Quick Connect (MCP Presets)
│   ├── [Connect GitHub]
│   ├── [Connect Linear]
│   └── [Connect Slack]
└── Custom MCP Servers
    └── [+ Add MCP Server URL]
```

---

## 9 Open Questions

1. Do we expose editing of an Agent's internal allow-list from the UI v1, or keep it API-only until permissions/multi-tenant story lands?
2. How to cancel a long-running Agent loop mid-flight from the canvas? Signal handling & UI affordance TBD.

---

## 10 Change-log

_2025-05-25_ – Document created from design discussion (davidrose, GPT-4o-assistant).
_2025-05-25_ – Updated Phase C to use MCP instead of custom plugin system. Added MCP integration strategy section.
