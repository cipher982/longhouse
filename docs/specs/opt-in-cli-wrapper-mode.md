# Opt-In CLI Wrapper Mode

Status: Active
Owner: activation / onboarding
Updated: 2026-04-02

## Goal

Make default-launcher wrappers a clear, explicit Longhouse feature instead of a hidden install-time side effect.

Users should be able to:

- keep using `claude` / `codex` normally by default
- opt in when they want bare `claude` / `codex` launches to route through Longhouse
- inspect, remove, and bypass that behavior easily

## Product Truth

Wrapper mode is not the product.

The product is still:

- imported sessions become findable first
- Longhouse sessions become controllable second

Wrapper mode is an activation accelerator for the second beat. It reduces the memory burden of typing `longhouse` first, but it should never be required for first value.

## Rules

- Install must stay non-invasive by default.
- `longhouse wrap --install` is the explicit opt-in.
- Wrapper mode must be passthrough-first.
- If Longhouse launch setup fails, the wrapper must fall back to the native CLI.
- Users must have an obvious escape hatch:
  - `LONGHOUSE_BYPASS=1 claude ...`
  - `longhouse wrap --uninstall`
- Public copy should call these **default-launcher wrappers**, not "managed-local shims."

## Supported Shape

### Default behavior

- The installer installs `longhouse`.
- Onboarding helps users import sessions and start Longhouse sessions.
- Nothing rewrites `claude` or `codex` automatically.

### Opt-in behavior

- `longhouse wrap --install`
- `longhouse wrap --install --provider claude`
- `longhouse wrap --status`
- `longhouse wrap --uninstall`

These wrappers should only intercept the simple interactive launch path and pass everything else through to the upstream binary.

Interactive onboarding may offer this as an explicit opt-in prompt. Quick/headless onboarding must keep skipping wrapper install by default.

## What Counts As Pass-Through

The wrapper must defer to the native CLI for:

- `auth`, `login`, `logout`, `config`, `help`, `version`, update commands
- non-interactive or pipe-style flags
- invocations with extra args that are not the bare launch path
- non-TTY contexts

## Done When

- The one-liner installer no longer auto-installs the old Claude shim path.
- Public docs and onboarding describe wrappers as optional and explicit.
- `longhouse wrap` copy uses Longhouse-session language instead of `managed-local` language.
- The CLI session launch commands also use the new public wording.
- Legacy shim-era repo artifacts are removed or clearly deprecated.
- Backend CLI tests cover the wrapper/onboarding wording that users actually see.

## First Slice

- remove installer-side automatic shim install
- remove the legacy shim script from the repo
- surface `longhouse wrap --install` in onboarding and docs
- clean up user-facing CLI wording

## Later

- richer wrapper status output
- broader provider support if more CLIs gain real Longhouse launch paths
