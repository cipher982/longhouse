# Longhouse Truth Plane Contracts

This is the launch-critical contract map for session truth. A contract here is
not a framework: it is a named product question, one backend projection that
answers it, and tests that prove the important transitions.

The goal is a user-facing invariant: when a session says live, offline,
read-only, running, closed, steerable, or queued, web and iOS are reading the
same backend-owned truth instead of reconstructing it differently.

## Contract Map

| Contract | Product Question | Canonical Surface | Current Proof | Next Proof |
| --- | --- | --- | --- | --- |
| Session identity and mode | Is this Shadow, Helm, or Console, and does Longhouse own control? | `session_state.mode` plus `session_state.control.ownership`, projected from durable launch/acquisition provenance | `server/tests_lite/test_session_state_contract.py` and kernel capability tests | Project 2 stores these facts directly in the bounded catalog. |
| Activity and presentation | What is the provider doing, and what scoped label should the user see? | `session_state.activity` plus versioned `session_state.presentation`; `runtime_display` is a deprecated facts-only alias | server property/contract tests plus web/iOS state-facts tests | Delete the alias after the compatibility window. |
| Timeline card status | Can I trust this session at a glance? | `session_state.presentation.primary` and independent access/transcript labels | state contract truth table plus client presentation tests | Add only orthogonal fact combinations, never combined statuses. |
| Action availability | Can this exact operation run now, and why not? | `session_state.control.actions`; legacy capability booleans are deprecated facts-only aliases | state contract and command-time exact-grant tests | Carry catalog lease generation through every command audit. |
| Session input lifecycle | What state is a submitted user input in after send, retry, crash, or cancel? | `SessionInput.status` plus typed intent/status/outcome fields on the input API | server input API/idempotency/boot-recovery tests plus web/iOS optimistic-row identity reconciliation tests | Add end-to-end queue replay proof if recovered queued rows ever gain a separate dispatcher. |
| Host and transport health | Is the host reachable, is the control transport alive, and are those different? | independent `session_state.host` and `session_state.control` facts | `server/tests_lite/test_session_liveness_facts.py` and state-contract tests | Add reason codes only from new raw evidence. |
| Remote launch lifecycle | Did a remote launch start, become live, fail, or become orphaned? | `project_remote_launch_lifecycle()` over durable `SessionLaunchAttempt`; legacy `AgentSession.launch_*` shims are not product truth | launch route tests plus lifecycle transition matrix and backend-owned failure copy | Add shared launch fixtures if clients start branching on launch states beyond displaying backend text. |
| Provisional vs durable transcript | Is this text live preview, durable archive, stale preview, or superseded? | `SessionTranscriptPreview` and durable events | preview freshness tests plus shared web/iOS rendering fixtures | Keep stale/superseded render decisions backend-owned as bridge behavior changes. |
| Clock and freshness | When does a signal expire, and which clock owns that decision? | backend freshness windows near runtime/provisional projections | `server/tests_lite/test_session_freshness_contract.py` pins backend-clock boundaries for runtime sync and provisional previews | Add cases here when a launch-critical projection introduces a new freshness window. |
| Error taxonomy | Which failures are product states versus logs/debug details? | typed response fields on launch/input/runtime projections | launch lifecycle normalizes user-visible error codes and messages; input/send/preview reason codes are typed at projection boundaries | Expand only when web, iOS, or agents branch on a new code. |

## Non-goals

- No generic contract DSL, registry, or proof engine.
- No formal contracts for auth, billing, model selection, tools, connectors,
  distribution, search ranking, or LLM heuristics unless a launch-critical
  session truth flow directly needs them.
- Client compatibility for old payloads must be inert defaults, not alternate
  product truth reconstructed from raw facts.
- No duplicated contract maps in client code. Clients may keep compatibility
  fallbacks, but the backend projection is the product contract.

## Enforcement Standard

Each contract earns its place by having:

1. One backend projection or model field group that owns the answer.
2. Stable reason codes only when clients or agents branch on them.
3. A truth-table or transition test for the states users can observe.
4. At least one client fixture when web/iOS rendering could diverge.
5. Explicit non-goals so the contract does not become a general framework.
