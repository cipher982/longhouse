---
name: provider-fidelity
description: Work on whether a provider's transcript reaches the served timeline intact. Load for provider transcript normalization, reasoning or thinking blocks, commentary vs final answer, turn end, tool intent, transcript shape census, the provider parity matrix, or managed_providers.yml dispositions, and before proposing a new census, matrix or coverage tracker.
---

# Provider fidelity

Longhouse's promise is that a managed session shows what the provider's own
terminal showed. The instruments for checking that already exist. Look here
before building a census, a feature matrix or a coverage report.

**Current state is not in this file.** Run the command; read the owner.

## Where each question is answered

| Question | Look at |
|---|---|
| Which transcript signals does each provider give us, which do we ingest, which are served? | `make provider-parity`; contract in `control-plane/docs/specs/provider-signal-parity.md` |
| Does provider X support operation Y (launch, steer, resume, a signal)? What is its disposition and canary? | `schemas/managed_providers.yml` (`transcript_signals`, `transcript_signal_surfaces`, per-operation `disposition`) |
| Did a provider update start writing a record shape nobody classified? | `scripts/qa/transcript_census.py` and the append-only catalog `schemas/transcript_shapes/` (one JSON per provider) |
| Is real usage on a host writing a shape the catalog does not classify? | `unrecognized_shapes` in `GET /api/agents/ingest-health` (`server/zerg/services/shape_canary.py`); the private Sauron job `longhouse-shape-canary` pages on it |
| How much of a provider's transcript content reaches the served timeline? | `make provider-fidelity-coverage` (`ARGS="--all-live"`), `make fidelity-coverage-gate`; design in `control-plane/docs/specs/provider-fidelity-harness.md` |
| Where does the engine keep or drop a transcript record (thinking, commentary, tool calls, turn end)? | `engine/src/pipeline/parser.rs` (`extract_assistant_events` and the per-provider extractors); signal definitions in `control-plane/docs/specs/provider-transcript-signals.md` |
| Where does the live reply text stop compared with the terminal? | `make terminal-truth`; `control-plane/docs/specs/terminal-truth-baseline.md` |
| How are tool calls tiered, translated and collapsed? | `config/tool-tiers.json`, `control-plane/docs/specs/tool-call-parity.md` |

## How to work

1. Name the signal and the provider, then find its row in `make provider-parity`
   output. The three stages are upstream support, ingest and surface.
2. A change to what a provider supports is a change to
   `schemas/managed_providers.yml` first; generated contracts follow
   (`make validate-managed-provider-contracts`).
3. A new record shape is classified in `schemas/transcript_shapes/`, not
   silently skipped in the parser. Add the golden fixture the signal's
   `surface_proof` or `.facts.expected.json` names.
4. Prove a parser change with `make test-engine TEST=<filter>`, then
   `make provider-parity` for the stage it moves. Live provider proofs run on
   the bench or the factory, never a provider CLI on this Mac (`managed-provider-cli` says why).

## Mistakes already made

- A fresh agent proposed building a census and a feature matrix. Both exist
  (rows 1 and 3 above). Read the table before writing a plan.
- Treating "the parser skips it" as a bug in the parser. First check whether the
  signal is declared `upstream_absent` or `not_implemented` in
  `managed_providers.yml`; the disposition is the intended behavior.
