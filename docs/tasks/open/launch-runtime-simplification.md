# Runtime Story Simplification

Status: In progress
Spec: `docs/specs/launch-runtime-simplification.md`
Last updated: 2026-03-17

## Goal

Make the launch story match the real product: sessions, conversations, Oikos, runners, and managed cloud sessions instead of bespoke autonomous agents or fiche-era concepts.

## Done when

- User-facing prompts, tools, and operator pages describe cloud work as managed CLI sessions.
- Provider support is documented honestly for archive, cloud start, continuation, hooks, and telemetry.
- Launch-facing UI/API naming is aligned around `cloud session` and automation-first wording.
- The deletion path for the current Oikos harness is explicit and underway.

## Checklist

- [x] Remove user-facing `commis` / autonomous-agent / server-first wording from prompts and product copy
- [x] Publish one honest provider capability story
- [x] Rename launch-facing cloud work labels to `cloud session` or equivalent
- [x] Remove obviously stale fiche/dashboard-era primary-path language
- [ ] Define and begin the deletion path for `OikosService` / `oikos_react_engine` / runner-facing harness seams

## Notes

- This task intentionally excludes the browser-vs-machine auth split that was being worked in parallel.
- The remaining work is architectural boundary cleanup, not another copy sweep.
