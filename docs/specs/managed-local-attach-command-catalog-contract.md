# Formal Plan: Helm managed-local launch / catalogd attach_command

Status: Implemented (Sol-refined, 2026-07-13)

## Problem

Cursor/Antigravity Helm launches fail before the provider TUI starts because
catalogd rejects `attach_command=""` while the product contract requires empty
attach for non-reattachable transports. The API then erases the real error into
a fake retryable 503.

## Contract

- `attach_command` remains a **required string** on `session.launch.local.create.v2`.
- `""` means no host reattach command (valid for Cursor Helm / Antigravity).
- Non-empty values must be ≤ 4096 characters.
- Reject `null`, missing field, wrong type, length > 4096.
- Catalogd validates wire shape only; provider-specific attach *shape* stays in
  Runtime Host response contract (`session_chat_impl`).

## Scope

In:

1. catalogd local-launch validation accepts empty attach.
2. Managed-local hot-path catalog error mapping is typed (this route only).
3. Real CatalogDaemon RPC tests + live-catalog managed-local regression.

Out:

- Deferred/TUI-first Helm registration
- Soft-launch without durable catalog identity
- New attach-policy helper / new error-mapper module
- remote_session_launch error-policy refactor
- Provider contract / attach command generation changes
- Search/WAL remediation

## Error mapping (managed-local catalog persist only)

| Exception | HTTP | Detail |
|---|---|---|
| `CatalogUnavailable` | 503 | unavailable / deadline; retry |
| `CatalogRemoteError` `conflict` | 409 | conflict message |
| `CatalogRemoteError` retryable | 503 | catalog message |
| `CatalogRemoteError` other (incl. invalid_request) | 500 | catalog message (server-built payload) |
| unexpected | 500 | generic persist failure |

## Implementation

1. `server/zerg/catalogd/server.py` — empty attach accepted on local launch RPC
2. `server/zerg/routers/session_chat.py` — typed managed-local catalog error mapping
3. `server/tests_lite/test_catalogd_launch.py` — empty/invalid/missing attach RPC coverage
4. `server/tests_lite/test_managed_local_launch.py` — real CatalogDaemon cursor+codex path + rejection surfacing
