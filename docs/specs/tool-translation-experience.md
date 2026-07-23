# Tool Translation Experience

Status: workshopping; council-refined direction
Owner: Longhouse timeline
Related: `tool-call-parity.md`, `timeline-reading-experience.md`

## Decision

Longhouse should be the shared **reading language** across coding agents without
becoming a universal ontology or intent engine.

The default timeline uses a small, stable vocabulary for what happened. Exact
provider identity, execution method, and raw evidence remain immediately
available. Longhouse may simplify how activity reads; it must never simplify
what the record says.

This is not dumbing down the archive. Raw transport can itself be a distortion:
in the audited Codex patent session, 23 stored wrappers obscured 37 meaningful
operations. Power-user design means fast access to truth, not forcing every
user to read protocol plumbing all the time.

## Three layers

### 1. Archive truth

Write-once provider evidence: source lines, raw JSON, call IDs, inputs, outputs,
and media. It stays provider-shaped, lossless, exportable, and authoritative.
Nothing in this epic rewrites it.

### 2. Canonical logical events

Deterministic, versioned structural recovery: call/result pairing and reviewed
wrapper unwrapping with links back to the enclosing evidence. Provider-native
tool names survive here. Synthesized children are marked as extracted, never
passed off as stored provider events.

This layer may recover structure; it may not guess intent, concurrency, child
ordering, or result attribution. Provider adapters own structural recovery
because they understand provider wire formats.

### 3. Disposable presentation

Read-time semantic labels, salience, and exploration grouping. The shared
projection layer maps canonical events into the Longhouse reading vocabulary.
It is freely re-versioned and never consumed as archive authority.

```text
archive truth -> canonical logical events -> semantic presentation
```

## Default experience

Prose is the document; tool activity is supporting evidence:

> Searched 3 sessions · Read 4 files · Listed 1 directory

> Called Longhouse · `session_tail` · 2 sessions

> Ran `git status`

> Edited `patent-a-slides-tiny.html`

Use one existing disclosure path:

1. The collapsed row tells the coherent story.
2. Expand reveals every canonical call/result in evidence order.
3. The detail drawer shows exact input, output, timing, provider, execution
   method, enclosing wrapper, provenance, and raw evidence.
4. A transcript-level **Raw archive** view supports whole-session forensics and
   export. Do not build a second semantic/raw timeline mode in v1.

Provider character remains visible through the provider identity on groups and
the real tool/method in details. Longhouse does not imitate provider TUI labels
as its product language.

## Vocabulary boundary

The initial presentation vocabulary is intentionally closed:

- `Searched`
- `Read`
- `Listed`
- `Ran`
- `Edited`
- `Viewed`
- `Called`
- `Asked`

Adding a verb requires a spec change and evidence that it describes an
observable operation rather than inferred agent intent. Do not grow this into
`Planned`, `Understood`, `Reviewed`, or other psychological/workflow claims.

Exploration grouping and semantic translation are one presentation mechanism,
not competing systems. Structural unwrapping runs first; unwrapped shell calls
then use the existing fail-closed shell-salience classifier and tool-tier config.

## Confidence and provenance

- **Exact:** explicit in provider evidence. No confidence badge.
- **Parsed:** produced by a deterministic, fixture-backed rule. Mark subtly in
  details, including the original method (`Read AGENTS.md · via sed`).
- **Unknown:** render the provider tool name or `Ran shell command` prominently
  and expose raw input. Never invent a cleaner label.

Every Parsed rule requires positive and adversarial conformance fixtures. Track
Exact/Parsed/Unknown rates by provider so format drift fails visibly instead of
silently degrading the product.

## Promotion gate

A wrapper may recede and its children may appear as canonical calls only when:

- every displayed child has deterministic identity and source provenance;
- every attributed result has evidence-backed pairing;
- all children and results remain reachable through the next disclosure level;
- incomplete, failed, or unattributed evidence stays visible;
- presentation does not claim concurrency or stronger ordering than the wire.

Otherwise, keep the wrapper prominent. Emission batches are wire facts;
parallel execution is not inferred.

## Never hide by default

- failures, nonzero exits, and unattributed results
- pending, dropped, orphaned, or partially parsed calls
- approvals and user-input requests
- edits, external side effects, and other consequential actions
- evidence that transcript or translation is incomplete

Successful waits, tool discovery, empty results, and recovered wrappers may
recede only when the promotion gate passes. They remain in Raw archive.

## Trust contract

1. Raw provider evidence is sacred and exportable.
2. Canonical structure is deterministic, versioned, and provenance-linked.
3. Semantic meaning is a labeled, disposable read-time lens.
4. Longhouse never invents intent, concurrency, ordering, or attribution.
5. Unknown behavior fails open visually: show more raw, never prettier lies.
6. A user can catch Longhouse being wrong in one gesture.

## Prerequisite

Fix the machine events contract independently of this UX decision. Codex `exec`
inputs are JavaScript strings, while the current response model accepts only
dictionaries. The archive is complete, but machine event/detail endpoints fail
validation and cannot serve as a trustworthy forensic path until corrected.

## Rejected defaults

- **Raw-only:** faithful to transport, hostile to understanding.
- **Provider-terminal imitation:** familiar but inconsistent and coupled to
  changing provider chrome.
- **Persisted universal semantics:** turns a reading convenience into archive
  authority and creates an unbounded ontology.
- **LLM-based intent inference:** nondeterministic and impossible to trust as
  evidence.

## Governing principle

**Canonicalize structure; translate only at read time. When unsure, show more
raw, never prettier lies.**
