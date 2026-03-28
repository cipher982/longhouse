# AI Ops Watchman

Status: active
Owner: David / Longhouse ops
Updated: 2026-03-28

## Problem

Today we mostly learn about Longhouse failures from lagging infrastructure symptoms like Netdata swap alerts, container RSS spikes, or manual log spelunking. That is too late and too low-level for the actual failure modes we care about.

The bad `david010` Codex replay incident is the clearest example:

- the first useful signal was not "swap is full"
- the real signal was "an ended session is still growing fast and accumulating impossible lineage"
- the app had enough raw evidence to notice that well before the host was in trouble

We want an always-on system that behaves more like a human operator watching the product than a pile of threshold rules.

## Working Framing

The product is not "more alerts." The product is an AI watchman:

- it gathers raw evidence continuously
- it compares what changed against recent history
- it asks a strong model whether the current story looks normal or dangerous
- it only escalates when it can point to concrete evidence

This is intentionally AI-first. We are not trying to enumerate every corruption pattern, replay mode, queue failure, or growth anomaly up front.

## Product Principles

### 1. AI-first, not threshold-first

The model should be the primary anomaly detector.

We still keep a tiny safety net of dumb health checks under it, but the watchman itself should reason over evidence, not a brittle rules maze.

### 2. Raw-first evidence

Do not pre-chew everything into bespoke anomaly features.

The core storage contract should stay minimal:

- when was this observed
- what entity does it describe
- where did it come from
- what raw payload did we see

The model should be able to see raw DB snapshots, file stats, and diagnostic excerpts instead of only derived booleans.

### 3. Tenant-local first

V1 should monitor the Longhouse instance from inside the app using state it already owns or can read locally:

- DB / WAL growth
- session growth and lineage weirdness
- ingest freshness
- write pressure where available
- recent incident history

Host-wide cross-tenant observability can come later from the control plane or a separate host agent. V1 should still catch the class of product failures that caused the recent incident.

### 4. Evidence before escalation

The watchman should not page because one number twitched.

Alerts should include:

- what changed
- why it looks abnormal
- exact evidence rows or excerpts
- the likely failure mode
- a suggested next action

### 5. Cost visibility is part of the product

Every watchman analysis run must record:

- input tokens
- output tokens
- total tokens
- reasoning tokens when available
- estimated USD cost

Input tokens matter most for recurring monitoring cost, so they are first-class fields, not a detail buried in JSON.

### 6. No autonomous destructive action in V1

The watchman can observe, explain, persist incidents, and notify.

It should not restart services, quarantine data, or mutate tenant state automatically in the first version.

## Non-Goals

- No attempt to replace Netdata or basic infrastructure monitoring
- No giant host-observability platform in this repo
- No huge manual rules engine for every anomaly class
- No silent auto-remediation in V1
- No dependence on external secret-manager specifics in repo code

## V1 Outcome

Longhouse should ship with a builtin AI watchman that can:

1. collect raw tenant-local operational observations on a schedule
2. persist them for short-horizon comparison
3. ask Grok 4.1 to judge whether the recent story looks normal or dangerous
4. persist the analysis run and its token/cost usage
5. open or update an `OperationalIncident` when warranted
6. send an operator email when the model has concrete evidence

## Model Policy

For this task, the watchman uses Grok 4.1.

V1 policy:

- smoke validation uses direct xAI Grok 4.1 if available
- app integration uses a dedicated watchman model id, defaulting to Grok 4.1
- the analyzer prompt should request structured JSON output
- temperature stays low for stable operational judgments

We do not hardcode a second "threshold engine" beside the model. The prompt, evidence window, and escalation policy are the core behavior.

## V1 Architecture

### 1. Observation collection

Every watchman run gathers append-only observations and stores them with minimal structure.

Initial observation sources:

- database file size and WAL size
- recent sessions with unusually large recent event growth
- ended sessions updated after end
- branch / lineage counts per session
- distinct source-path counts per session
- open operational incidents

Optional source when available:

- recent app log excerpt related to ingest or serializer pressure

### 2. Observation store

Add a small append-only table for raw observations.

Proposed shape:

- `id`
- `observed_at`
- `window_start_at`
- `window_end_at`
- `entity_type`
- `entity_id`
- `source`
- `payload_json`
- `payload_text`

This is enough for history, replay, and prompt reconstruction without over-modeling every anomaly type.

### 3. Watchman runs

Add a durable run table for each analyzer invocation.

Proposed shape:

- `id`
- `started_at`
- `finished_at`
- `status`
- `model`
- `prompt_version`
- `input_tokens`
- `output_tokens`
- `total_tokens`
- `reasoning_tokens`
- `estimated_cost_usd`
- `result_json`
- `error`

This gives us a clean cost and behavior ledger without overloading unrelated job metadata.

### 4. Analyzer service

The analyzer service:

1. loads a bounded recent observation window plus a compact historical baseline
2. renders those observations into a prompt
3. calls Grok 4.1 for a structured judgment
4. validates the JSON result
5. persists the watchman run with usage and cost

Expected result shape:

- `status`: `normal` | `watch` | `critical`
- `title`
- `summary`
- `evidence`
- `suspicious_entities`
- `incident_type`
- `dedupe_key`
- `should_email`
- `recommended_action`

### 5. Incident + notification bridge

If the model returns `watch` or `critical`:

- open or update an `OperationalIncident`
- dedupe by analyzer-provided key
- store the analysis summary and evidence in incident context

If `should_email` is true:

- send an operator alert email through existing SES helpers

### 6. Scheduler integration

Register a builtin periodic job for the watchman.

V1 should start conservatively, for example every 5 minutes, with the observation window sized so token cost stays bounded.

## Prompt Contract

The prompt should frame the model as a skeptical, evidence-driven ops analyst.

Rules:

- default to `normal` when the evidence is weak
- never invent missing facts
- escalate only with explicit evidence
- prefer short operational summaries
- focus on what changed in the recent window
- distinguish mature-tenant anomalies from normal new-tenant growth when the evidence supports that distinction

## Cost Contract

The watchman must record provider usage from the response object, not estimate token counts from prompt length.

V1 cost behavior:

- store provider-reported token usage on every successful model call
- compute estimated USD cost from the pricing catalog when direct provider cost is unavailable
- make input tokens queryable directly

## Rollout Plan

### Phase 0: Formalize

- write this spec
- create an active task file
- add one `TODO.md` entry

### Phase 1: Real-call smoke

- add a standalone smoke script that calls Grok 4.1 with a tiny watchman-style prompt
- require structured JSON output
- print or persist input/output token usage
- verify the script does not crash on the real provider path

### Phase 2: App integration

- add watchman observation and run models
- add collection helpers for the V1 evidence sources
- add analyzer service
- add incident + email bridge
- register builtin job

### Phase 3: Verification

- unit tests for collectors, result parsing, incident dedupe, and cost accounting
- smoke run against a real model
- local app verification

### Phase 4: Ship

- commit atomic slices
- push to `main`
- deploy the runtime image path
- verify the job runs cleanly on a live instance

## Done When

- A formal spec and active task file exist.
- A real Grok 4.1 smoke script runs successfully and records input-token usage.
- Longhouse can persist raw watchman observations and watchman analysis runs.
- A builtin watchman job can analyze recent observations without crashing.
- Non-normal analyses open or update `OperationalIncident` rows.
- Alert-worthy analyses can send SES email through existing helpers.
- Token and estimated cost tracking are durable and queryable for every watchman LLM run.
