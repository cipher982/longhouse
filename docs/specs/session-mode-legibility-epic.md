# Session-Mode and Provider-Contract Legibility

**Status:** Spec draft — reviewed by hatch cursor grok and hatch codex sol
(2026-07-21), revised. Ready for founder go/no-go, not yet started.
**Owner:** Longhouse core
**Created:** 2026-07-21
**Related:** `ARCHITECTURE.md`, `docs/specs/managed-session-state-normalization-epic.md`,
`docs/specs/turn-scoped-console-execution.md`, `docs/specs/rust-edge-provider-parity.md`,
`docs/specs/cursor-opencode-console-parity.md`, `docs/specs/cursor-console-native-turns.md`,
`schemas/managed_providers.yml`

## Why this exists

A 2026-07-21 investigation into provider-launch capability gaps (Claude/Cursor
`run_once=false`, the Claude Helm remote-launch reaper question) took two AI
agents and the founder multiple passes to even correctly state the questions.
The underlying facts were not obscure — every answer was in the repo — but
nothing in the repo's structure pointed a reader at the right place, and the
vocabulary used to describe the same concepts diverges across several
independent locations that all read as authoritative. This is the predictable
output of many independent agents each doing genuinely good work and each
choosing their own place and words to put it, with nothing structurally
forcing consolidation.

This spec is scoped to *legibility*: can a reader with zero context correctly
reconstruct the session-mode model and current provider support matrix from
the repo alone, in one pass, without git archaeology.

**Revision note:** the first draft of this spec proposed a same-phase
repo-wide rename/restructure. Both reviewers independently pushed back on
that as too large and, in one place, factually wrong about the target data
model. This revision splits the work into a small, low-risk Phase A (below)
and defers the larger structural change to a Phase B that is bundled with the
real behavioral work it can't honestly be separated from.

## Root causes, with evidence (revised after review)

1. **The product vocabulary and the code vocabulary were never mapped.**
   Shadow/Helm/Console is defined in prose one level up, in the workspace
   `AGENTS.md` — Longhouse has no in-repo `AGENTS.md` of its own (only
   `.cursor/rules/claude.mdc`, which points at the parent file). The code
   speaks a different, older vocabulary: `run_once`, `launch_local`,
   `launch_remote`, `turn_start`. No docstring, field name, or module banner
   states the mapping. **Sharper than the original diagnosis (per grok):** a
   cold clone of the *public* repo never sees the product trichotomy at all —
   it only exists in workspace-level context that isn't part of the
   repository.

2. **`ARCHITECTURE.md`'s glossary is incomplete against the current model,
   not strictly wrong.** (Revised: sol is right that "Managed/unmanaged" is
   a real, still-valid control-ownership axis referenced in `AGENTS.md`
   itself — it isn't a false statement. But it's missing the newer
   Shadow/Helm/Console vocabulary entirely, and `ARCHITECTURE.md` is the
   second thing a cold reader opens per this repo's own `docs/README.md`.
   Also newly found: `.agents/skills/managed-provider-cli/SKILL.md`, hop two
   in the agent discovery path, teaches the same incomplete binary split —
   this is a second location with the same gap, not a one-off.

3. **One capability field's meaning depends on unwritten context, and even
   the prose disagrees with the fields.** `claude.run_once=false` means "no
   adapter built yet." `cursor.run_once=false` means "deliberately retired
   field, ignore it, real support lives under `turn_start`." Both render as
   an identical bare `false`. **New finding (grok):** it's worse than
   ambiguous — Cursor's own `operation_evidence.run_once` prose says
   "unavailable until…" while `turn_start` for Cursor is `true` and working.
   The written evidence and the working implementation contradict each other
   in the same file.

4. **A parity-test system already exists for exactly this problem, and it has
   already drifted.** (Substantially revised — this was the single biggest
   correction from review.) The original draft proposed *adding* a
   behavioral contract test as new work. Sol found that one already exists:
   `docs/specs/rust-edge-provider-parity.md` specified it,
   `engine/src/control_channel.rs:3111-3184` implements a bidirectional
   parity test, and `docs/specs/cursor-opencode-console-parity.md` marks it
   complete. **It doesn't actually catch the problem** because it compares
   the manifest against a second, hand-maintained shadow table
   (`ENGINE_DISPATCH_SUPPORTS`) instead of the real production dispatch
   router — and that shadow table is *itself* already stale: it still lists
   `cursor.run_once` as supported at `control_channel.rs:2570`, while the
   manifest correctly omits it and the production handler
   (`control_channel.rs:908-915`) rejects every non-Codex `run_once` request.
   This is direct proof that "write a parity spec once" does not stay true —
   the fix has to make one registry authoritative, not add a third table that
   will drift the same way.
   Separately, the narrower claim from the original draft —
   `opencode.run_once=true` — is real and still live: `run_once_supported_providers()`
   consumes it and `remote_session_launch.py:84-85` uses that set, so it is a
   genuine stale server-level claim, just not the *machine_control_supports*
   false-advertisement the existing engine test already guards against.

5. **Two of three "Claude launch" code paths are genuinely under-named; the
   third already isn't.** (Revised — sol's correction.) `console_turns.py`
   is already explicitly Console-named and provider-neutral; it isn't part of
   the naming confusion. The real problem is narrower and sharper than
   originally stated: `_run_native_claude_tui` (physical terminal) and
   `_launch_detached_native_claude_channel` (remote-dispatched, invoked only
   via `claude_channel.py`) sit in the same file with no naming convention
   distinguishing which real-world scenario each implements.

6. **Two generations of the same concept coexist with no lifecycle marker,
   and this is partly deliberate mid-migration state, not pure neglect.**
   `turn-scoped-console-execution.md` already schedules deletion of
   `execution_lifetime`, `run_once`, and legacy launch booleans after the
   `turn_start` migration. They're still present as apparent equals. Any
   cleanup work must not read "legacy" fields as safe to delete outright —
   some are mid-migration scaffolding with an already-agreed removal trigger,
   not simply forgotten.

7. **The same field name means two different things in two places.**
   `can_resume` on a session connection (derived from `reattach`) and
   `can_resume` on a provider contract (continuation eligibility) are
   different concepts sharing one identifier, acknowledged by a comment in
   `managed_provider_contracts.py` but not by the name itself.

8. **New: the generated JSON is not the source of truth; the schema is, and
   the original draft targeted the wrong file.** `schemas/managed_providers.yml`
   is authored; `server/zerg/config/managed_provider_contracts.json` is
   generated from it via `scripts/generate_managed_provider_contracts.py`.
   Any legibility or schema-shape work has to land in the YAML, not the JSON.

9. **New: there is live, uncommitted work on the exact files this phase would
   touch.** A `worktree-companion-claude-print` branch has in-progress
   changes to `engine/src/control_channel.rs` and
   `schemas/managed_providers.yml` — the same two files any dispatch-registry
   or Claude-adapter work here would edit. This phase cannot start blind to
   that; it needs a check-in with whoever owns that work before touching
   either file, or it will collide.

## Design principles (unchanged from draft, both reviewers endorsed)

- One canonical concept explanation; everything else links to it, nothing
  restates it in its own words.
- Placement matches discovery order (`AGENTS.md` → skills → `ARCHITECTURE.md`/
  repo tree → code), not authoring convenience.
- A capability claim without a proof it is still true is worse than no claim.
- Convention alone does not hold across ~79 specs and multiple independently
  committing agents — this needs CI enforcement, not a norm to remember.

### Operating reality: this project is agent-first, with no human safety net

This is not a preference, it's a constraint the design has to be built around.
There is one founder and no human code-review layer — every change in this
repo, including this phase's own changes, is written and reviewed by AI
agents. That changes what "done" means for this phase specifically:

- **A norm, a doc convention, or a naming standard is not a fix by itself.**
  It is only a fix once something automated enforces it, because the actual
  failure mode this spec exists to close is "a competent agent, acting in
  good faith, quietly drifted from the standard" — proven three times over in
  this investigation alone (the stale `ARCHITECTURE.md` glossary, the
  `opencode.run_once` false claim, and the already-drifted
  `rust-edge-provider-parity` shadow table that was itself supposed to
  prevent drift). A human reviewer would have caught some of these on sight;
  this repo doesn't have one, so the tests have to.
- **Every deliverable in Phase A that can be a CI check must be a CI check,**
  not a written guideline an agent is expected to remember next session. Where
  Phase A only proposes a lint/allowlist, treat that as the minimum bar, not
  the target — prefer a hard-failing contract test over a soft lint wherever
  the two are both feasible.
- **This generalizes past this one spec.** The root failure — declared
  capability drifting silently from actual behavior — is a category, not a
  one-off. Wherever this phase's dispatch-registry fix (Phase A item 3)
  establishes a pattern for "one authoritative source, verified automatically
  against real behavior instead of a hand-maintained shadow table," that
  pattern is the template other contract-truthfulness gaps in this repo
  should eventually follow, not a one-time fix scoped narrowly to providers.
- **Prefer integration/end-to-end proof over unit-level trust.** A unit test
  that both the manifest and the dispatch table individually parse doesn't
  prove they agree with each other, and didn't catch the `cursor.run_once`
  drift. Contract tests in this area should exercise the real
  request/dispatch path (or as close to it as hermetically possible) rather
  than asserting properties of each side in isolation.

## Data model: adapter-scoped, not a single flattened enum

**This supersedes the original draft's proposal**, which suggested collapsing
each provider's capabilities into one `mode` string per field. Sol's review
correctly identified that this would recreate the exact conflation problem
root cause #3 complains about, just with nicer-looking names — it mixes three
independent axes onto one value:

- **Product mode** — Helm or Console (Shadow is an observation/provenance
  state, not an execution mode, and likely doesn't belong in this manifest at
  all).
- **Support lifecycle** — supported, unsupported, retired/legacy.
- **Adapter/mechanics** — which concrete implementation handles it (channel
  bridge, PTY, print invocation, app server) — and these are genuinely
  different per provider, not just differently named, per
  `managed-session-state-normalization-epic.md`'s own provider-mechanics
  table.

Target shape is adapter-scoped declarations, e.g.:

```yaml
provider: claude
adapters:
  - id: claude_channel
    mode: helm
    launch_surfaces: [local, remote]
    operations: [send, interrupt, steer, reattach]
  # claude_print / a Console adapter appears here once implemented, not before
```

`support_status`, `proof_level`, and legacy/retirement state stay separate
fields, not folded into `mode`. Notably, `managed-session-state-normalization-epic.md`
already sketches close to this shape (`launch_modes`, `supported_operations`,
`proof_level` as separate fields) — this phase should converge on that
existing design rather than invent a fourth vocabulary.

## Phase A — now, docs/status only, zero behavior or rename risk

Scoped tightly to what's safe to do without touching dispatch, engine
behavior, or public identifiers. Executed from an isolated worktree
(`session-mode-legibility`), so there is no working-tree collision with any
other branch; `control_channel.rs` and `schemas/managed_providers.yml` are
still touched in items 2-3 below where the fix is genuinely a legibility fix
(a stale/contradictory claim), not because of a coordination requirement with
other in-progress branches. Any eventual merge-time conflict with unrelated
work on those files is ordinary git, resolved at merge time.

1. Rewrite `ARCHITECTURE.md`'s glossary to include Shadow/Helm/Console
   accurately, sourced from and linking back to the canonical explanation
   (kept in `ARCHITECTURE.md` itself — see decision below). Update
   `.agents/skills/managed-provider-cli/SKILL.md` to link instead of
   redefine. **Shipped:** `fee189fb7` (ARCHITECTURE.md), `2cea77c13` (skill
   link).
2. Fix the two concretely-wrong claims found in review: remove/correct
   `opencode.run_once=true` in `schemas/managed_providers.yml` (or
   explicitly justify it as an intentional compatibility shim, in writing, if
   it turns out something still depends on it), and reconcile
   `ENGINE_DISPATCH_SUPPORTS` so it no longer lists `cursor.run_once`.
3. Make the existing `rust-edge-provider-parity` test compare against the
   real production dispatch registry instead of the second hand-maintained
   `ENGINE_DISPATCH_SUPPORTS` table — one authoritative registry, not a
   parallel one that can drift again.
4. Add module-level docstring banners to `_run_native_claude_tui` and
   `_launch_detached_native_claude_channel` stating plainly which product
   mode each implements, and a short mapping comment/table linking
   field-names (`run_once`, `turn_start`, `launch_local`, `launch_remote`) to
   product vocabulary. **No public symbol, JSON field, or Typer command
   renames in this phase** — private helper renames are fine if trivially
   reviewable in the same commit as the banner.
5. Add a **hard-failing CI check**, not an advisory lint, that fails the build
   if a second location in the repo redefines Shadow/Helm/Console or
   Managed/Unmanaged in its own prose instead of linking to the canonical
   section — an explicit allowlist of where the definitions may originate,
   everything else must link and CI verifies it, not just style-guide it.
6. Coordinate with the `worktree-companion-claude-print` work before editing
   `control_channel.rs` or `schemas/managed_providers.yml`, to avoid a silent
   collision.

Acceptance for Phase A requires both a manual and an automated check — a
read-through alone is not sufficient given there's no human reviewer to
repeat it next time a spec changes:

- **Manual:** a reader starting only from `AGENTS.md` can, within two hops,
  correctly state what Shadow/Helm/Console mean, which of Claude/Codex/
  Cursor/OpenCode support which mode today, and where each implementation
  lives — without git history and without finding two documents that
  disagree.
- **Automated:** CI fails on (a) a declared provider capability that the
  dispatch registry doesn't actually support, in either direction, and (b) a
  second prose definition of the mode vocabulary outside the allowlisted
  canonical location. Both checks must run on every PR, not just at phase
  completion, so this can't silently re-drift the way the original
  `rust-edge-provider-parity` guardrail did.

## Phase B — deferred, bundled with real behavioral work

Explicitly **not** part of this phase; recorded here so it isn't lost and
isn't attempted piecemeal:

- The adapter-scoped schema v2 migration (real restructure of
  `managed_providers.yml`).
- Deleting the legacy `execution_lifetime`/`run_once`/launch-boolean fields
  per `turn-scoped-console-execution.md`'s already-agreed trigger.
- Public renames of the Claude launch functions/CLI surface.
- Building the actual Claude Console (`turn_start`) adapter.
- Deciding whether Cursor gets `launch_remote`.
- Deciding an idle-timeout/reaper policy for Claude Helm-remote sessions.

Reviewer rationale for deferring rather than doing now (sol): doing the
schema restructure and the renames before the behavioral work means touching
the same manifest, engine router, tests, and Claude launch code twice — once
to rename, once to actually add the capability — with two separate chances to
regress it. Do them together when Phase B is scoped.

## Non-goals (unchanged)

- Any change to Antigravity — explicitly deprioritized by the founder.
- A full `docs/specs/` sprawl cleanup (79 files) — out of scope.

## Decisions (previously open questions — both reviewers converged)

- **Concept doc location:** rewritten `ARCHITECTURE.md` glossary, not a new
  `docs/concepts/` directory. Both reviewers agreed a new top-level location
  adds a third thing to learn instead of fixing the two that exist. The
  workspace `AGENTS.md` gets a short pointer into it, not a restatement —
  addressing that the product trichotomy currently doesn't exist in the
  public repo at all.
- **Provider-agnostic mode enum:** shared product modes only
  (`shadow | helm | console`), with adapter mechanics and support lifecycle
  kept as separate fields — sol's adapter-scoped model, not the flatter
  single-enum version from the first draft.
- **Claude launch-path rename scope:** docstrings, module banners, and a
  vocabulary mapping table now; public symbol/JSON/CLI renames deferred to
  Phase B, paired with the behavior change that motivates touching that code
  anyway.

## Guardrails against the rename making things worse

(Both reviewers flagged this as the main execution risk; codified as
constraints, not suggestions.)

1. Docs/status/test-only commits first; zero runtime behavior changes in the
   same commits.
2. CI fails if both the old and new vocabulary are taught as current anywhere
   in the allowlisted definition locations.
3. No public identifier renames in Phase A.
4. The dispatch-registry fix (item 3 above) lands before any manifest
   reshape, so a false `true` can't quietly become "fixed by renaming" instead
   of fixed by being correct.
5. Explicit review checklist for Phase A PRs: diff must be
   docs/schema-status/tests only unless labeled otherwise.
6. Acceptance is a scripted cold-reader check (start from `AGENTS.md` only),
   not a subjective read-through.
