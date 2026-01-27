# QA Fiche Job

Purpose: run deterministic system checks, then let the LLM analyze and summarize.

- **Hybrid determinism**: `collect.sh` gathers stable data; the fiche analyzes results.
- **State preservation**: previous `qa_state` from `ops.runs.metadata` is reused on failure/parse errors to avoid false "all clear."
- **Chronic alerts**: only newly chronic issues trigger alerts; prior state is the reference.
