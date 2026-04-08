# Prelaunch Simplification Cut Plan

Status: active
Owner: launch hardening
Updated: 2026-04-07

## Goal

Finish the prelaunch simplification pass around one honest product loop:

- import or launch real CLI sessions
- find them fast from one timeline
- steer them later on machines the user owns

Anything that does not materially strengthen that loop should be frozen, hidden, or deleted before launch.

## Product Decision

The launch product is:

- session sync and memory
- remote control over agents running on user-owned machines

The launch product is **not**:

- magical local-to-cloud takeover
- a separate cloud-branch product
- a mailbox product
- a jobs platform
- a broad autonomy platform

Hosted may exist later as another explicit launch target, but it should not be part of the core launch promise.

## How To Use This Plan

- **Keep** means launch-critical. Invest here.
- **Freeze** means stop expanding it, hide it if needed, and only fix real breakage.
- **Cut** means remove the user-facing promise and delete code if the removal is cheap enough before launch.

Rough code sizes below are direct feature files only. They do **not** include generated types, broad shared plumbing, or docs/copy knock-on effects. They are meant for ranking, not accounting.

## Ranked Cut Order

### 1. Cloud-branch product story and execution-home taxonomy

Recommendation: **Cut from the launch story, then delete in code as far as safe**

Why:

- It creates a second product promise next to the stronger machine-owned-execution story.
- It forces the UI, backend, and tests to reason about session types instead of session capabilities.
- It is the largest direct removal win that is still near the core surface.

Direct surface measured:

- backend: about 4.3K LOC
- frontend: about 1.3K LOC
- tests: about 2.5K LOC
- total direct surface: about 8.1K LOC

What to do:

- stop promising `branch-cloud` as part of launch
- collapse user-facing capability language to:
  - live control
  - host reattach
  - search-only
- remove `managed_hosted` / `cloud_takeover` from user-facing product language
- stop expanding tests that encode cloud-branch semantics

What to keep:

- transcript sync
- live send/control for Longhouse-launched sessions
- explicit future path for launching on another machine later

Risk:

- **Medium**
- this touches core session UX, but the simplification is aligned with the product you actually want to sell

### 2. Jobs as a user-facing product surface

Recommendation: **Freeze or cut**

Why:

- Jobs are not part of the launch wedge.
- They add pages, secrets UI, repo sync behavior, and backend complexity that do not help the main story.
- If Longhouse is not launching as "your jobs platform," this surface is expensive distraction.

Direct surface measured:

- about 5.6K LOC

What to do:

- remove jobs from primary nav and product docs
- keep only the builtin maintenance jobs required for Longhouse itself
- defer or cut user-facing jobs repo management and jobs settings UX

Risk:

- **Medium**
- operationally useful, but weak product leverage before launch

### 3. Loop inbox, proactive operator, and conversation/email surfaces

Recommendation: **Freeze hard; cut visible product promises**

Why:

- These are interesting, but they are a second product layered on top of the first one.
- They create onboarding, auth, transport, and UI complexity that does not help the core session-control loop.
- They are easy to keep dogfooding forever if not explicitly deprioritized.

Direct surface measured:

- loop inbox direct surface: about 2.3K LOC
- Gmail auth/connect surface: about 1.0K LOC direct, with larger hidden complexity behind it
- proactive operator spec/docs are substantial even where direct code is still modest

What to do:

- keep them dogfood-only or behind flags
- remove them from launch docs, navigation, and claims
- stop broadening the transport matrix before launch

Risk:

- **Low to medium**
- mostly product scope risk, not platform risk

### 4. Briefings, insights, and reflection as first-class product surfaces

Recommendation: **Freeze or hide**

Why:

- Search and recall already deliver the main memory value.
- Briefings and reflection are useful, but they are not required to prove the wedge.
- They add more surfaces to maintain and explain.

Direct surface measured:

- about 1.5K LOC direct

What to do:

- keep the raw underlying memory/search primitives
- remove or down-rank dedicated pages from the launch story
- avoid making curated memory a separate product promise before launch

Risk:

- **Low**

### 5. Presence and forum leftovers

Recommendation: **Simplify aggressively**

Why:

- `/forum` already redirects to the timeline, which is a strong signal that the product does not want a second live-view home.
- Remaining presence/cache/styling surface is overhead unless it clearly strengthens the main timeline.

Direct surface measured:

- about 1.4K LOC direct

What to do:

- keep only the presence data that materially improves timeline/live-control UX
- delete leftover forum-specific routing, copy, and styling

Risk:

- **Low**

### 6. North-star docs and launch copy

Recommendation: **Cut now**

Why:

- Product ambiguity in docs leaks directly into code churn and test churn.
- The docs were still carrying too many pivots, futures, and alternative stories at once.

Measured bloat:

- `VISION.md` was 1607 lines
- `AGENTS.md` was 364 lines

What to do:

- keep one short vision doc
- keep one lean repo execution doc
- keep one explicit prelaunch cut plan
- remove cloud-takeover language from launch-facing docs unless it is clearly marked as later or non-launch

Risk:

- **Low**

## Keep Investing Here

These are the surfaces most likely to matter at launch:

- session ingest quality
- timeline, session detail, search, and recall
- machine-facing coordination primitives
- managed-local remote execution and control
- runner reliability on user-owned machines
- clear capability language in UI and API contracts

## Do Not Spend Prelaunch Time Here Unless The Product Decision Changes

- deep control-plane refactors
- broad autonomy/operator expansion
- email/inbox polish
- user-facing jobs platform work
- new execution-home taxonomies
- trying to make cloud takeover feel magical

## Suggested Sequencing

1. Rewrite the north-star docs and launch language.
2. Hide or de-emphasize non-core routes from nav and product copy.
3. Remove the cloud-branch capability model from the launch UX and tests.
4. Delete the now-orphaned backend/frontend code paths.
5. Only then decide whether any frozen surfaces deserve to survive as dogfood-only features.

## Expected Win

If you follow this cut order, the realistic prelaunch removal opportunity is on the order of:

- **8K+ LOC** from cloud-branch semantics alone
- **5K+ LOC** from jobs/product-surface code
- **3K-5K+ LOC** from loop inbox, email, briefings, and adjacent launch-discourse surface area

That is enough to materially reduce bug-report surface area without touching the core ingest, timeline, runner, or managed-local control loop.
