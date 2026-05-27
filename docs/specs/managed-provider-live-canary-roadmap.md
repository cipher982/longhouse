# Managed Provider Live Canary Roadmap

Status: Draft
Owner: Machine Agent + managed provider CLI surfaces
Updated: 2026-05-27
Related: `managed-provider-control-matrix.md`, `provider-cli-contracts-and-codex-release-canaries.md`

## Purpose

The hermetic provider-control canary proves Longhouse's local control code. It
does not prove that the latest upstream provider release still honors the
provider surface Longhouse depends on.

This document defines the missing layer: real upstream release probes and the
promotion rules for full provider support.

## Current Dogfood Evidence

Local versions observed on David's machine:

| Provider | Local Version | Installed Control Family |
| --- | --- | --- |
| Codex | `codex-cli 0.134.0` | app server bridge |
| Claude Code | `2.1.152` | native channel / MCP bridge |
| OpenCode | `1.15.11` | local HTTP server bridge |
| Antigravity | `1.0.2` | hook inbox |

Dogfood health on 2026-05-27 proves operation advertisement but not full
shipping health. The control channel advertised:

```text
codex: send, interrupt, steer, launch, continue
claude: send, interrupt, steer, launch
opencode: send, interrupt, launch
antigravity: send
```

Provider release status is interpreted against the installed local version.
Newer upstream artifacts remain visible as candidate release status, but do
not degrade local health until the installed provider version matches the
artifact or is newer than the newest reviewed artifact.

## Control Plane Families

Do not build a generic "agent app" adapter that assumes every provider behaves
like Codex. Longhouse should use a shared registry for facts and
provider-specific code for behavior.

The release canary color is evidence maturity, not a provider support tier.
Claude channel control is first-class even when its scheduled live-token canary
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
   active; scheduled live-token proof verifies upstream mid-turn behavior.
5. Idle steer rejected at the Runtime Host API before dispatch.
6. Interrupt sends a graceful SIGINT to the real Claude session process.

### Hook Inbox Plane

Provider: Antigravity.

Antigravity's stable surface is hooks. Longhouse can queue input and have active
hooks claim it at provider-defined loop boundaries. That is send support, not
interrupt or active-turn steer.

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

## Next Implementation Slices

1. Add a provider live-canary dispatcher that can run one provider or all
   providers and emit one Sauron-facing artifact per provider. The dispatcher
   currently covers OpenCode, Claude, and Antigravity at
   `scripts/qa/provider-live-canary.py`.
2. Extend the Claude lane beyond the initial no-token checks. The current lane
   proves binary identity, redacted auth shape, required launch/session flags,
   hidden `--channels` tagged-channel parsing, and macOS PTY wrapper availability,
   but the release-drift artifact intentionally remains Yellow until scheduled
   live-token evidence proves the full channel contract. That Yellow status is
   not a downgrade from first-class Claude channel control.
   The operator live POC at `make managed-claude-poc` can now run an optional
   delayed `intent=steer` injection with `ARGS="--steer-text ..."` and requires
   the assistant transcript to contain the expected steered response. Detached
   remote launch still needs a repeatable live gate against a healthy Runtime
   Host.
3. Extend the OpenCode server probe from the initial no-token lane. It is the
   lowest-risk live canary:
   `opencode serve --hostname 127.0.0.1 --port 0 --pure`, `/global/health`,
   `/doc`, session create, attach `--help` command shape, and abort are checked
   without relying on a visible terminal or prompt execution. A later
   token-spending lane must verify `prompt_async` execution before the
   provider-live verdict can be Green; the current lane verifies the endpoint is
   present in OpenCode's OpenAPI document and therefore stays Yellow.
4. Extend the Antigravity canary from its current yellow state -- real `agy`
   version/help/plugin validate/install/list plus Longhouse global-hook config
   proof -- to loop-level hook behavior against the upstream runtime.
5. Fold the existing Codex release canary into the same artifact shape without
   deleting its Codex-specific app-server checks.

## Reference Surfaces

- Codex: stock `codex app-server`, `codex --remote`, engine bridge relay.
- Claude Code: `claude --channels server:longhouse-channel`,
  `claude-channel serve/send/interrupt`, and Runtime Host active-turn gating.
- OpenCode: `opencode serve`, `opencode attach`, server `/global/health`,
  `/doc`, `/session`, `/session/:id/prompt_async`, and
  `/session/:id/abort`.
- Antigravity: `agy` plugin hooks, `PreInvocation`, `PostInvocation`, `Stop`,
  `injectSteps`, and `terminationBehavior: "force_continue"`.
