# Managed Provider Live Canary Roadmap

Status: Current contract
Owner: Machine Agent + managed provider CLI surfaces
Updated: 2026-05-28
Related: `managed-provider-control-matrix.md`, `provider-cli-contracts-and-codex-release-canaries.md`

## Purpose

The hermetic provider-control canary proves Longhouse's local control code. It
does not prove that the latest upstream provider release still honors the
provider surface Longhouse depends on.

This document defines the missing layer: real upstream release probes and the
promotion rules for full provider support.

## Shipping Shape

Provider release status is interpreted against the installed local version.
Newer upstream artifacts remain visible as candidate release status, but do
not degrade local health until the installed provider version matches the
artifact or is newer than the newest reviewed artifact. Local live-proof
sidecars are additive operation evidence for this machine; they do not rewrite
or suppress the Sauron release verdict.

## Control Plane Families

Do not build a generic "agent app" adapter that assumes every provider behaves
like Codex. Longhouse should use a shared registry for facts and
provider-specific code for behavior.

The release canary color is evidence maturity, not a provider support tier.
Claude channel control is first-class even when its live-token canary
is Yellow; Yellow means "manual/source/hermetic proof exists, but the automated
release-drift proof is not yet Green."

### App Server Plane

Providers: Codex, OpenCode.

These providers expose a long-lived local server with documented or discoverable
session operations. Longhouse can support launch, send, interrupt, reattach, and
runtime probes when the upstream server surface remains stable.

Required canaries:

1. Binary identity: prove Longhouse is testing the stock provider binary.
2. Server startup: prove the current binary starts the expected local server.
3. Schema probe: prove required endpoints still exist.
4. Session create/attach: prove provider session identity can be created and
   reattached.
5. Send/interrupt: prove the real endpoints accept Longhouse payloads.
6. Transcript/runtime binding: prove Longhouse can bind provider runtime state
   back to the managed Longhouse session.

### Channel Plane

Provider: Claude Code.

Claude exposes a channel/MCP path that Longhouse can use for send, active-turn
steer delivery, and interrupt. This is first-class control, but it is not the
same shape as an app-server bridge.

Required canaries:

1. Channel bridge handshake with the real Claude binary.
2. Detached launch readiness using the PTY wrapper path Longhouse ships.
3. Send payload delivery over `notifications/claude/channel`.
4. Runtime Host dispatches channel steer only when runtime phase is fresh and
   active; live-token proof verifies upstream mid-turn behavior.
5. Idle steer rejected at the Runtime Host API before dispatch.
6. Interrupt sends a graceful SIGINT to the real Claude session process.

### Hook Inbox Plane

Provider: Antigravity.

Antigravity's stable surface is hooks. Longhouse queues input through a hook
inbox adapter, and machine-control send is advertised after a real upstream
`agy` loop proved active hooks claim queued input at provider-defined loop
boundaries. That is send support, not interrupt or active-turn steer.

Required canaries:

1. Plugin install writes the Longhouse hook config expected by the current
   provider release.
2. `PreInvocation` claims pending input and emits `injectSteps`.
3. `PostInvocation` claims pending input and emits `force_continue`.
4. `Stop` continues when queued input is waiting.
5. Transcript/runtime binding writes the expected local evidence.
6. Real upstream `agy` loop canary proves the hook responses still affect the
   provider loop, not only Longhouse's generated hook script.

## Promotion Rules

An operation can be advertised only when all applicable gates pass:

1. The shared manifest declares the intended operation.
2. The shared manifest carries per-operation evidence under
   `operation_evidence`; first-class target support and proof level are separate
   facts.
3. Provider-specific execution code exists.
4. Hermetic Longhouse control E2E passes.
5. Real upstream release probe passes or the operation is explicitly marked
   "source-reviewed only" in the provider release artifact.
6. Dogfood local-health exposes the operation in `control_operations_by_provider`.
7. Runtime Host rejects unsupported operation intents instead of silently
   falling back to a weaker behavior.

No provider should move from "send" to "steer" because its send path happens to
work while an agent is busy. Steer needs active-phase proof and idle rejection.

Local live proof and Sauron release status are separate feeds. Sauron release
artifacts answer "is this upstream release reviewed enough to recommend or
block?" Local proof artifacts answer "has this machine proven operation behavior
for the installed CLI version?" A matching local proof artifact can strengthen
the operation evidence shown by local-health, but it cannot turn a Yellow/Red
Sauron release verdict Green or satisfy the source-drift release gate by
itself. Shared provider release-profile artifacts must include top-level
operation evidence so each unsupported operation, source-reviewed operation,
and missing live release proof is machine-readable.

## Implemented Proof Loop

`longhouse provider-live publish` runs the shared no-token live canaries for
Claude, OpenCode, and Antigravity, then publishes stable local sidecars under
`~/.longhouse/provider-live-proof`. Codex stays in its dedicated release canary
lane because its app-server/TUI bridge proof is provider-specific.

Antigravity's shared no-token live proof includes binary/help shape, plugin
validate/install/list, global hook config, and a script-level hook-inbox claim
cycle for `PreInvocation`, `PostInvocation`, and `Stop`. The send promotion is
owned by the explicit release canary:
`scripts/qa/provider-control-e2e-canary.py --provider antigravity --antigravity-real-agy-send`.
That canary spends a real `agy --print` turn and only passes when a marker that
is absent from the prompt is injected through `PreInvocation` and appears in the
model-visible response.

`POST /api/agents/machines/{device_id}/provider-live-proof` dispatches the
typed `provider.live_proof` command only to machines advertising
`{provider}.live_proof`. Release automation can pass
`expected_provider_version`; mismatches are typed application conflicts.
Runtime Host also deduplicates in-flight proofs per owner/device/provider.

`make dogfood-refresh` publishes local sidecars, runs hosted route proof through
the Runtime Host -> Machine Agent path, and writes the latest route artifact to
`~/.longhouse/provider-live-route-e2e/latest.json`. `longhouse local-health`
and `longhouse doctor` display local provider proof and hosted-route proof as
separate signals.

## Reference Surfaces

- Codex: stock `codex app-server`, `codex --remote`, engine bridge relay.
- Claude Code:
  `claude --dangerously-load-development-channels server:longhouse-channel`,
  `claude-channel serve/send/interrupt`, and Runtime Host active-turn gating.
- OpenCode: `opencode serve`, `opencode attach`, server `/global/health`,
  `/doc`, `/session`, `/session/:id/prompt_async`, and
  `/session/:id/abort`.
- Antigravity: `agy` plugin hooks, `PreInvocation`, `PostInvocation`, `Stop`,
  `injectSteps`, and `terminationBehavior: "force_continue"`.
