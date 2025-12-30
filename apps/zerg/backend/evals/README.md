# Zerg Eval Dataset (Hermetic Regression Tests)

This folder contains a lightweight eval harness for prompt + tool regression testing.

## Run

From repo root:

```bash
make eval
```

This runs `apps/zerg/backend/evals/` in **hermetic mode** (the test suite stubs the LLM so no network calls happen).

## Add / edit test cases

- Edit or add YAML files under `apps/zerg/backend/evals/datasets/`.
- Each `case` runs a supervisor task and then applies assertions to the captured metrics.

Supported assertions (Phase 1):
- `contains` (substring match on supervisor result text)
- `regex` (regex match on supervisor result text)
- `status` (success / failed / deferred)
- `latency_ms` (max/min bounds)
- `total_tokens` (max bound; from `agent_runs.total_tokens`)
- `worker_spawned` (exact/min/max; from `worker_jobs` rows correlated to the supervisor run)
- `tool_called` (checks durable run events for tool lifecycle events, e.g. `supervisor_tool_started`)

## Notes

- `--variant` is accepted (Make passes `--variant=baseline`) but variants are only meaningful once datasets define variant configs and/or the runner gains prompt override support.
- If you want semantic prompt regression (real LLM behavior), use the existing live prompt suite: `make test-prompts TOKEN=...` (requires dev stack running).
