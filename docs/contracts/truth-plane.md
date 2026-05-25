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
| Session identity kernel | What kind of control relationship does Longhouse own for this session? | `KernelSessionCapabilities` in `server/zerg/services/agents/kernel_capabilities.py` | `server/tests_lite/test_session_kernel_capabilities.py` | Keep web/iOS from inferring ownership from raw metadata. |
| Runtime display | What is happening right now, and how trustworthy is that signal? | `SessionRuntimeDisplayResponse` from `server/zerg/services/session_runtime_display.py` | runtime display tests plus shared web/iOS fixtures that assert exact label/headline/detail/tone rendering from backend payloads | Expand fixture cases as new runtime states appear. |
| Timeline card status | Can I trust this session at a glance? | `timeline_card.status` and `border_tone` from backend timeline projection | `server/tests_lite/test_timeline_status_presentation.py` | Treat web/iOS fallback labels as legacy compatibility only. |
| Send affordance | Can the user type, what happens on send, and why is send disabled? | `SessionCapabilitiesResponse` fields `input_mode`, `default_input_intent`, `composer_enabled`, `send_disabled_reason` | `server/tests_lite/test_send_affordance_truth_table.py` | Web/iOS should use the typed reason for display branches instead of local heuristics. |
| Session input lifecycle | What state is a submitted user input in after send, retry, crash, or cancel? | `SessionInput.status` plus typed intent/status/outcome fields on the input API | server input API/idempotency/boot-recovery tests plus web/iOS optimistic-row identity reconciliation tests | Add end-to-end queue replay proof if recovered queued rows ever gain a separate dispatcher. |
| Host and transport health | Is the host reachable, is the control transport alive, and are those different? | runtime facts plus capabilities/send affordance projection | `server/tests_lite/test_session_liveness_facts.py` and `server/tests_lite/test_session_input_presentation.py` host/control matrix cases | Add web/iOS fixtures that assert clients only render the backend capability/display fields. |
| Remote launch lifecycle | Did a remote launch start, become live, fail, or become orphaned? | `project_remote_launch_lifecycle()` over durable `SessionLaunchAttempt`; legacy `AgentSession.launch_*` shims are not product truth | launch route tests plus lifecycle transition matrix and web/iOS state fixtures | Keep launch-state labels/reasons backend-owned as new states appear. |
| Provisional vs durable transcript | Is this text live preview, durable archive, stale preview, or superseded? | `SessionTranscriptPreview` and durable events | preview freshness tests plus shared web/iOS rendering fixtures | Keep stale/superseded render decisions backend-owned as bridge behavior changes. |
| Clock and freshness | When does a signal expire, and which clock owns that decision? | backend freshness windows near runtime/provisional projections | scattered tests | Centralize freshness assertions around the projections that expose them. |
| Error taxonomy | Which failures are product states versus logs/debug details? | typed response fields on launch/input/runtime projections | partial route tests | Normalize user-visible reason codes only where clients branch on them. |

## Non-goals

- No generic contract DSL, registry, or proof engine.
- No formal contracts for auth, billing, model selection, tools, connectors,
  distribution, search ranking, or LLM heuristics unless a launch-critical
  session truth flow directly needs them.
- No client-side fallback removal for old payloads until the backend field is
  deployed and both web and iOS can tolerate it.
- No duplicated contract maps in client code. Clients may keep compatibility
  fallbacks, but the backend projection is the product contract.

## Enforcement Standard

Each contract earns its place by having:

1. One backend projection or model field group that owns the answer.
2. Stable reason codes only when clients or agents branch on them.
3. A truth-table or transition test for the states users can observe.
4. At least one client fixture when web/iOS rendering could diverge.
5. Explicit non-goals so the contract does not become a general framework.
