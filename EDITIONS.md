# Longhouse Editions

Longhouse is open-core.

This public repository contains the Apache-2.0 core product:

- Python CLI and package layer
- Runtime Host (`longhouse serve`): FastAPI API, bundled web UI, SQLite state
- Rust Machine Agent (`longhouse-engine`)
- macOS local status/setup surface
- iOS read/steer client
- Runner and machine-facing `/api/agents/*` contracts
- self-host documentation, install, repair, and upgrade paths

Longhouse Cloud is proprietary and lives outside this repository. It includes:

- hosted signup and account management
- billing, checkout, and webhook handling
- hosted instance provisioning and fleet operations
- production deployment automation and private operator dashboards
- abuse controls, quotas, and hosted-only infrastructure glue
- paid-provider pools or other shared cloud resources

The public Runtime Host can be configured to talk to a hosted control plane via
`CONTROL_PLANE_URL`. That is a service boundary, not a source-code dependency.
Self-hosted Longhouse does not need the control plane.

## Contributor Boundary

Contributions to this repository should strengthen the public core: session
ingest, timeline, search, recall, managed local control, machine APIs,
self-hosting, install/repair, and client surfaces over those contracts.

Do not add signup, billing, provisioning, fleet administration, or hosted
operator code to this public repository. Keep those changes in the private
Longhouse Cloud codebase.

## History And Artifacts

Removing hosted source from the current tree only protects future development.
Older public Git history, release archives, PyPI artifacts, and container images
may still contain files that were previously public. Treat that as a separate
artifact/history cleanup decision.
