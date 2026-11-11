# Longhouse Editions

Longhouse is open-core.

## This repository (Apache-2.0)

- Python CLI and package layer
- Runtime Host (`longhouse-server serve`): FastAPI API, bundled web UI, SQLite state
- Rust Machine Agent (`longhouse-engine`)
- macOS local status and setup app
- iOS read and steer client
- Runner and the machine-facing `/api/agents/*` contracts
- self-host docs, install, repair and upgrade paths

## Longhouse Cloud (proprietary, outside this repository)

- hosted signup and account management
- billing, checkout and webhook handling
- hosted instance provisioning and fleet operations
- production deployment automation and private operator dashboards
- abuse controls, quotas and hosted-only infrastructure glue

Hosted Longhouse runs the Runtime Host and control plane. Provider execution
still goes through each user's installed provider clients and existing accounts;
hosted plans do not bundle or resell model inference.

A public Runtime Host can talk to a hosted control plane through
`CONTROL_PLANE_URL`. That is a service boundary, not a source dependency, and
self-hosted Longhouse does not need a control plane.

## Contributor boundary

Contributions should strengthen the public core: session ingest, timeline,
search, recall, managed local control, machine APIs, self-hosting, install and
repair, and client surfaces over those contracts. Do not add signup, billing,
provisioning, fleet administration or hosted operator code here. Those changes
belong in the private Longhouse Cloud codebase.

## History and artifacts

Removing hosted source from the current tree protects only future development.
Older Git history, release archives, PyPI artifacts and container images may
still contain files that were once public. Cleaning those is a separate decision.
