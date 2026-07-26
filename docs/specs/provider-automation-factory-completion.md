# Provider Automation Factory Completion

**Status:** implemented; one live canary outstanding
**Owner:** Longhouse
**Updated:** 2026-07-26
**Extends:** `provider-automation-factory-epic.md`
**Scope:** Finish the factory for Claude, Codex, Cursor, and OpenCode. Move
Antigravity to maintenance.

## Decision

The factory epic declared Phases 0–2 implemented. That is true for the
transcript stream and for four of five providers. It is not true for the
control stream, and it is not true for Cursor at all.

This document closes the epic by separating three things the contract currently
conflates into one free-text field:

1. whether an operation is implemented, and if not, whose problem that is;
2. how strongly the implementation is proven; and
3. what would advance the proof, and why that has not happened.

These are independent. An operation can be implemented and unproven, proven and
policy-disabled, or absent upstream and therefore permanently both. Today all
three collapse into a `level` plus an unschematized `next:` string, which is why
"what is left?" is not a question anyone can answer.

## What is actually missing

### Cursor is absent from the factory

Cursor shipped as a first-tier native provider on 2026-07-26 (`b733fa90d`). It
appears in none of the four provider registries:

```text
scripts/qa/provider-release-proof.py           SUPPORTED_PROVIDERS
scripts/qa/provider-release-proof-old-new.py   SUPPORTED_PROVIDERS
server/zerg/qa/universal_agent_harness.py      SUPPORTED_PROVIDERS
server/zerg/provider_live_proof.py             SUPPORTED_LIVE_PROOF_PROVIDERS
```

There is no `cursor_release_identity.py` beside the four that exist, and no
`CursorHarnessAdapter` in `ADAPTER_CLASS_BY_PROVIDER`. Cursor's only proof is
two bespoke scripts a human runs by hand. A new `cursor-agent` release today
produces no verdict, no old/new diff, and no signal to Sauron.

The four hand-maintained tuples are the mechanism that produced this. The parent
epic already forbids a duplicate provider registry, and `provider_configs()`
gestures at the fix — it unions `managed_provider_names()` with
`SUPPORTED_PROVIDERS` and then iterates only the tuple, making the union dead
code (`universal_agent_harness.py:7794`). Adding `cursor` to four tuples would
fix today's symptom and leave the mechanism intact.

### The control stream is hermetic almost everywhere

Twelve operations across five providers give 60 cells
(`managed_provider_contract_manifest.py:43`). `live_token` appears twice. The
factory proves that Longhouse constructs the right argv and reduces the right
events. It largely does not prove that the provider did the thing.

That is defensible on cost grounds, but it is currently indistinguishable from
"we never got to it," because the only record is 30 free-text `next:` hints.

### The universal runner is accumulating provider branches

`_action_support` hardcodes `{"claude", "codex", "opencode"}` for permission
prompts and special-cases Claude and Antigravity for external event channels
(`universal_agent_harness.py:5319`); `_provider_pause_tool_name` carries a
per-provider `if` ladder. The epic forbids this. Each branch is a place a new
provider is silently wrong rather than explicitly unsupported.

## Three axes

Keep `level`. Add a disposition and a promotion record. Do not merge them.

### Axis 1 — disposition (implementation truth)

Reuse the vocabulary that already exists for capabilities
(`provider_capability_contract.py:13`), rather than inventing a second one:

| Value | Meaning |
| --- | --- |
| `implemented` | Longhouse implements this operation for this provider. |
| `not_implemented` | Longhouse could implement it; it has not. This is the backlog. |
| `upstream_absent` | The provider exposes no such surface. Never attempt. |
| `policy_disabled` | The provider may support it; Longhouse deliberately routes it elsewhere. |

This is the unification worth doing: one implementation vocabulary shared by
`capabilities[].disposition` and `operation_evidence[].disposition`, enforced by
one validator constant. It is additive — no existing capability value changes,
so the evaluator and its locked tests are untouched.

Unifying the *evidence* axis with the capability axis would be a breaking change
and is explicitly rejected. `CapabilityDisposition` gates product enablement
(`provider_capability_contract.py:96`), and an implemented-but-unproven
capability is deliberately expressed as `disposition=implemented` plus
`verification=missing|stale|failed`. Those are different questions and stay
separate.

### Axis 2 — level (evidence maturity)

Unchanged: `none | source_review | hermetic | live_no_token | live_token`.

### Axis 3 — promotion (what would advance it)

Replaces the free-text `next:` string.

- `owner_action` — the named canary or scenario that would raise the level.
  Required whenever the level is below `live_no_token` and the disposition is
  `implemented` or `not_implemented`.
- `blocker` — `none | budget | upstream | policy`. `budget` means reachable but
  requiring funded token spend. This is a reason a promotion has not happened,
  not a state of the operation, which is why it is a field here rather than a
  disposition value.

### Invariants the validator enforces

These are the point of the exercise. Each catches a real contradiction present
in the contract today.

1. `upstream_absent` and `policy_disabled` require `level: none` and a `reason`.
2. `policy_disabled` additionally requires `routed_to`.
3. `upstream_absent` additionally requires `observed_provider_version`, so a
   stale absence is re-checked when release identity changes. This is how the
   factory learns that an upstream gap closed.
4. `implemented` requires `level` above `none`.
5. `not_implemented` requires an `owner_action`.
6. The provider's boolean flag and the disposition must agree: the flag is true
   if and only if the disposition is `implemented`.

Invariant 6 is what catches Cursor's `can_resume: false` sitting beside
`reattach` evidence describing proven native resume continuity.

## Per-provider truth

Upstream surfaces were re-verified against official docs and source on
2026-07-26 so they are not re-litigated.

### Blocked upstream — never attempt again

| Provider | Operation | Reason | Verified against |
| --- | --- | --- | --- |
| Cursor | `steer_active_turn` | ACP exposes `session/prompt`, `session/update`, and `session/cancel`, with no steer or inject method; `stream-json` is output-only; hooks do not inject into an in-flight generation. Longhouse Gate 0 independently records that injected input did not alter the active generation on `2026.07.23-e383d2b`. | cursor.com/docs/cli/acp, changelog through 2026-07-20 |
| OpenCode | `steer_active_turn` | No steer method on the server API. The Steer/Queue PR (anomalyco/opencode#26199) closed unmerged in 2026-06; mid-run delivery remains an open request (#32157). | opencode.ai/docs/server, 2026-07 |
| Antigravity | `reattach`, `interrupt`, `terminate`, `steer_active_turn`, `answer_pause`, `turn_start` | No stable provider surface. See maintenance below. | Longhouse contract |

Gate 0 proves that stock Cursor did not alter the active generation. It does not
prove the stronger claim that the input was queued into a later turn. The
recorded reason states only what the artifact supports.

### Routed elsewhere by design — not a gap

| Provider | Operation | `routed_to` |
| --- | --- | --- |
| Cursor | `answer_pause` | The pull-based Longhouse pause-request API via the fail-closed permission hook. Cursor does expose a permission reply, but only over ACP (`session/request_permission`), and Longhouse deliberately does not execute ACP — `cursor_acp` is a read alias for archived sessions only. Adopting it would replace the stock TUI and change the session mode, which Helm parity forbids. |
| Cursor | `run_once` | `session.turn.start`. Cursor Console has a real durable headless path: `start_cursor_print_turn` mints or resumes a provider thread and runs stock `cursor-agent` (`cursor_print.rs:69`), and `turn_start` is true. Only the legacy `session.run_once` command is Codex-only (`control_channel.rs:909`). |
| Claude | `run_once` | `session.turn.start` through `claude_print`. |

An earlier draft of this spec called Cursor `run_once` upstream-absent. That was
wrong, and the distinction matters: `upstream_absent` tells future agents to stop
looking, which would have been false. `policy_disabled` with a `routed_to` tells
them where the working path is.

This also names a live asymmetry: any client dispatching `session.answer_pause`
generically does nothing for Cursor. `routed_to` is how a client author finds
that out without reading the engine.

### Longhouse work — reachable and queued

| Provider | Operations at `implemented` with sub-live evidence |
| --- | --- |
| Codex | `send_input`, `interrupt`, `steer_active_turn`, `answer_pause`, `runtime_phase`, `transcript_binding`, `run_once` |
| Claude | `reattach`, `answer_pause`, `terminate`, `runtime_phase`, `transcript_binding`, `turn_start` |
| OpenCode | `tail_output`, `runtime_phase` |
| Cursor | The full ledger, pending promotion below |

Cursor is the special case: its ledger reads `hermetic` everywhere, but Gate 0
and the product E2E both passed live on 2026-07-26 against `2026.07.23-e383d2b`.
The evidence exists and was never recorded. Promotion is bookkeeping against
real artifacts, not new proof.

### New capability — OpenCode permission answering

The contract recorded OpenCode `answer_pause` as `none`, "unsupported until
OpenCode pause-answer semantics are proven." That was false on two counts.

Upstream, OpenCode's server exposes `POST /permission/:requestID/reply` with a
`{reply, message?}` body plus `permission.asked` and `permission.replied`
events, documented as of 2026-07-24. The semantics were already proven.

Inside Longhouse, a working reply path already existed:
`session_chat._resolve_opencode_permission_via_bridge` shelled out to the Python
`opencode_bridge.permission_reply` CLI from the Runtime Host. So this was never
"not built" — it was built on one route, never advertised as a machine-control
capability, and then described in the contract as an upstream limitation. That
is exactly the failure the disposition axis exists to prevent: prose that hides
both a working implementation and a real gap behind the same word.

The fix routes it through the standard managed-control dispatch instead, which
removes a Python CLI shellout from the Runtime Host, turns `answer_pause` and
`opencode.answer_pause` true, and keeps the fail-closed property: a dispatch
failure returns 502 and leaves the pause unresolved rather than reporting a
decision that never reached the provider.

Evidence stays `hermetic`. No live OpenCode permission canary has been run, and
`owner_action` names the one that would promote it. Do not mark this
`live_token` without running it.

## Disposition must reach the harness

Declaring dispositions changes nothing on its own. Unsupported contract booleans
currently become `unsupported_gap` unconditionally
(`universal_agent_harness.py:5427`), and `_action_support` never consults
operation evidence (`universal_agent_harness.py:5319`). Without this section the
headline outcome does not happen and Antigravity still drags the scorecard down.

Map disposition onto harness outcome:

| Disposition | Harness status | Verdict contribution |
| --- | --- | --- |
| `implemented` | as today, from evidence level | normal |
| `not_implemented` | `unsupported_gap` | counts as backlog |
| `upstream_absent` | `typed_unsupported_fact` | not a gap, never Yellow |
| `policy_disabled` | `typed_unsupported_fact` | not a gap, never Yellow |

Tests must prove that an `upstream_absent` cell does not produce a Yellow
verdict and that a `not_implemented` cell still does.

## Derive the registries

Delete all four hand-maintained tuples. Derive the supported set from the
contract, filtered by support tier, so a provider that exists in
`managed_providers.yml` is automatically in the factory.

Add `support_tier: launch | maintenance` to the contract. `launch` providers are
in every lane. `maintenance` providers keep ingest, archive, and transcript
scenarios and are excluded from control-proof lanes.

After this, `provider_configs()` iterates what it already unions, and the drift
mechanism that omitted Cursor cannot recur. A test asserts that every contract
provider at `launch` tier resolves an adapter.

## Contract-driven support

Delete the provider branches in the universal runner by moving the facts into
the contract:

| Current branch | Becomes |
| --- | --- |
| `permission_prompt` hardcodes three providers | `permission_prompt_surface: bool` |
| `external_event_channel` special-cases Claude and Antigravity | `external_event_channel: <name>\|null` |
| `_provider_pause_tool_name` if-ladder | `pause_tool_name: <string>` |
| `provider_configs()` hardcodes harness safety flags | `harness_safe_no_token_prompt`, `harness_real_managed_session_e2e` |

The bar is not "no provider name appears anywhere." Dispatching to a
provider-specific canary implementation is mechanics and belongs in the adapter.
The bar is that no function which *decides what a provider supports* branches on
a provider name: `_action_support`, `_provider_pause_tool_name`, and
`provider_configs` must be free of them, enforced by a test.

`provider_configs()` is the concrete reason this matters. Cursor silently got
`real_managed_session_e2e=False` and no permission surface because nobody added
it to the right hardcoded set — the failure mode is a new provider being quietly
wrong rather than explicitly unsupported.

## Cursor onboarding

Cursor joins the factory the same way any provider does. No Cursor-specific lane.

1. Register `CursorHarnessAdapter` in `ADAPTER_CLASS_BY_PROVIDER`. The adapter is
   an empty subclass like the other four; behavior comes from the contract.
2. Add `server/zerg/qa/cursor_release_identity.py` following the OpenCode
   pattern and register it in `provider_qualification._PROFILES`.

   The release-identity module assumes strict semver: `load_request` rejects any
   `expected_provider_version` failing `STRICT_SEMVER`
   (`provider_release_identity.py:135`), and `cursor-agent --version` reports
   `2026.07.23-e383d2b`, a calendar version whose `07` component has a leading
   zero and cannot match. Thread a per-profile version grammar through
   `IdentityProfile` and `load_request`, defaulting to strict semver so the
   existing four providers are unaffected. This is a prerequisite: without it a
   valid Cursor request cannot be accepted.
3. Fold the two bespoke canaries into named qualification profiles
   (`cursor_helm_gate0`, `cursor_helm_product_e2e`) reachable through the
   standard request/artifact interface, so a release trigger can drive them.

## Coordination parity

`launch_managed_cursor` (`engine/src/longhouse.rs:691`) shells to the engine and
never issues a coordination token or writes an MCP config. Claude, Codex, and
OpenCode all do. A `longhouse cursor` session therefore has no `peers`, `send`,
`tail`, `inbox`, `reply`, or `search_sessions`.

Cursor supports MCP natively and the launcher already passes `--approve-mcps`,
so this is a wiring gap, not a capability gap. Close it, then declare the four
`coordination.*` capabilities with the assertion shape Codex and Claude use.

`startup_coordination_context` stays false unless a hook-timing problem like
Codex's deferred post-compaction card is actually observed.

## Antigravity maintenance

Antigravity keeps ingest and stops consuming engineering attention.

- Its native entrypoint is already `excluded`; that stays.
- `support_tier: maintenance` removes it from control-proof lanes.
- Its unsupported operations become `upstream_absent` where the surface is
  genuinely missing and `not_implemented` where Longhouse simply never designed
  the policy — `run_once` ("not implemented yet") and `terminate` ("until
  process termination policy is designed") are the latter, and mislabelling them
  as upstream absence would be a lie the validator should reject.
- `AGENTS.md` gets one line so agents do not rediscover this and spend a day
  on it.

Nothing is deleted. Ingest, archive, and transcript projection keep working and
keep being tested. This is a statement about where time goes.

## Delivery phases

A vertical slice first. The taxonomy is validated against one real provider
before four more are migrated, so a wrong model costs one migration rather than
five plus their digests.

**Phase 1 — Derive registries, onboard Cursor under the current contract.**
Delete the four tuples, add `support_tier`, register the adapter, add the
version grammar and `cursor_release_identity.py`. Cursor enters every lane with
no schema change. Tests: five providers resolve adapters; a Cursor release
request produces an artifact.

**Phase 2 — Introduce the three axes for Cursor only.** Add `disposition`,
`reason`, `routed_to`, `owner_action`, `blocker`,
`observed_provider_version`; validator invariants; harness status mapping.
Validate one generated artifact and one capability decision end to end.

**Phase 3 — Migrate the remaining four providers.** Mechanical once Phase 2 has
proven the model. Regenerate `managed_provider_contracts.json` and the digest.

**Phase 4 — Delete the provider branches.** Contract-driven `permission_prompt`,
`external_event_channel`, and pause tool naming.

**Phase 5 — Cursor evidence and coordination.** Promote the ledger from the
2026-07-26 artifacts, settle `can_resume` under invariant 6, wire the
coordination MCP, declare the capabilities.

**Phase 6 — OpenCode permission answering.** New capability with a live canary.

**Phase 7 — Antigravity maintenance and doc reconciliation.** Support tier,
dispositions, AGENTS.md line. `cursor-helm-launch-parity.md` is superseded: its
capability-projection rules and the one outstanding Runtime Host outage
qualification move into `cursor-native-device-parity.md`, and the file is
deleted rather than left to contradict the qualified account.
`native-device-runtime.md` gains Cursor in the facade list.

**Phase 8 — Cutover.** Full validation, ship, dogfood refresh, and a factory
health report showing zero unclassified cells.

## Result

All 60 provider/operation cells are classified: 47 implemented, 6 Longhouse
work, 4 routed elsewhere by design, 3 absent upstream. Every remaining piece of
Longhouse work belongs to Antigravity, which is maintenance tier. Across the
four launch-tier providers there is no unimplemented control operation left.

`live_token` evidence went from 2 cells to 10, entirely by recording Cursor
proof that already existed on disk and had never been written down.

Two claims in this document were wrong when first written and are corrected
above rather than quietly edited away. Cursor `run_once` is `policy_disabled`
routed to `session.turn.start`, not `upstream_absent`; calling it upstream
absence would have told future agents to stop looking at a path that works.
OpenCode permission answering was not unbuilt; it existed on the hosted route
behind a Python CLI shellout while the contract described it as an upstream
limitation.

The one outstanding item is a live OpenCode permission canary. It is recorded as
`hermetic` with `owner_action: opencode_bridge permission.reply live canary`,
which is the state this schema exists to express honestly.

## Definition of done

1. All 60 provider/operation cells carry an explicit disposition; none is unclassified.
2. A completeness check proves contract operations, action definitions, and
   scenarios agree on the matrix, so a cell cannot exist in one and not another.
3. `not_implemented` cells carry a named `owner_action`, making "what is left" a query.
4. `upstream_absent` cells carry a reason and an observed provider version and are
   re-checked when release identity changes.
5. `upstream_absent` and `policy_disabled` do not produce Yellow verdicts;
   `not_implemented` does.
6. The supported-provider set is derived from the contract; no hand-maintained
   registry tuple remains.
7. Cursor has a release identity, qualification profiles, a harness adapter, and
   the same coordination surface as Claude, Codex, and OpenCode.
8. The universal runner body contains no provider equality checks.
9. Antigravity is maintenance-tier and stops appearing as unfinished work.
10. One truthful Cursor spec remains; the contradictory ones are gone.

## Open questions

Do not block earlier phases on these.

- Which `implemented` cells below `live_no_token` are worth funded token spend on
  a schedule, and which stay `blocker: budget` indefinitely? The taxonomy makes
  the question askable; it does not answer it. This is David's cost decision.
