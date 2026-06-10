# Model Capability Contract

Status: active

Longhouse has one model capability contract: `config/models.json` declares the
text and embedding providers the Runtime Host intends to use. Environment
variables only satisfy that contract; they do not define a second capability
model.

## Explicit Branches

- `TESTING=1` skips startup provider validation because tests patch models and
  providers directly.
- `LLM_DISABLED=1` is the explicit no-provider runtime. Background LLM and
  embedding work must stay disabled, and request-time LLM surfaces should fail
  or degrade honestly.
- `DEMO_MODE=1` may run without real providers.
- Otherwise, every provider declared by `config/models.json` must have its
  configured key present at startup. Missing keys are a boot error.

## Request-Time Behavior

Semantic search and recall depend on embeddings. They must never return `200`
with an empty result set when embeddings are unavailable or the nonempty session
corpus has no loaded embeddings. Return `503` with the upstream repair action:
set the configured provider key, remove the embedding declaration for an
intentional no-embedding runtime, or run `/api/agents/backfill-embeddings` after
fixing the embedding worker.

Lexical search remains the explicit non-embedding search path.

## Anti-Patterns

- Do not infer model readiness from broad env sniffing such as "any LLM key is
  present." Use `zerg.models_config`.
- Do not silently switch semantic requests to lexical search.
- Do not let MCP, browser, and CLI clients each derive capability state
  independently. The Runtime Host owns the capability truth.
