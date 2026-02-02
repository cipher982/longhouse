<p align="center">
  <img src="apps/zerg/frontend-web/branding/swarm-logo-master.png" alt="Longhouse" width="180" />
</p>

<h1 align="center">Longhouse</h1>

<p align="center">
  <strong>All your AI coding sessions, unified and searchable.</strong>
</p>

> Naming: Longhouse is the brand. Oikos is the assistant UI. Repo paths still use `zerg`.

---

## Quick Start (local)

```bash
pip install longhouse
longhouse onboard
```

This starts the server and opens your browser.

### Useful commands

- `longhouse serve` — start server (localhost:8080)
- `longhouse connect` — sync Claude Code sessions
- `longhouse onboard` — rerun wizard

---

## Dev (repo)

```bash
git clone https://github.com/cipher982/longhouse.git
cd longhouse
cp .env.example .env
bun install
make dev
# Open http://localhost:30080/timeline
```

Run tests:

```bash
make test
make test-e2e
```

---

## Status

Alpha. SQLite-first. Expect rough edges.

---

## License

ISC

---

<!-- onboarding-contract:start -->
```json
{
  "primary_route": "/timeline",
  "steps": [
    "cp .env.example .env",
    "docker compose -f docker/docker-compose.dev.yml --profile dev up -d --wait",
    "curl -sf --retry 10 --retry-delay 2 http://localhost:30080/health"
  ],
  "cleanup": [
    "docker compose -f docker/docker-compose.dev.yml --profile dev down -v"
  ],
  "cta_buttons": [
    {"label": "Load demo", "selector": "[data-testid='demo-cta']"}
  ]
}
```
<!-- onboarding-contract:end -->
