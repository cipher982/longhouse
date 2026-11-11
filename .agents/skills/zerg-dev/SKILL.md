---
name: zerg-dev
description: Run the Longhouse web UI locally (`make dev`, `make dev-demo`, `make stop`) and troubleshoot it. Use when starting the local frontend or choosing between real-account and demo data.
---

# Local dev

```bash
make dev        # frontend at http://localhost:47200, proxied to your linked Runtime Host
make dev-demo   # isolated local backend with seeded demo data
make stop
```

- `make dev` proxies API calls to the Runtime Host in `~/.longhouse/machine/target-url`
  using the existing device token, so it shows the real account. It needs a prior
  `longhouse auth --url <runtime>` link and never falls back to an empty local
  database. Ctrl+C stops it. It starts no local API; the demo backend on `:47300`
  is `make dev-demo`.
- Do not assume a dev stack is already running.
- `make stop` runs `pkill -f` on every `uvicorn zerg.main:app` and `vite` on the
  machine, not only yours. Do not use it while another session's stack may be up;
  stop your own PIDs.
- For rendering a page to a PNG without a running stack, use `zerg-ui`.
