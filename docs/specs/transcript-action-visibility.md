# Transcript action visibility

Status: shipped (2026-07-24)

Successor to `shell-activity-summaries-v2.md`. That spec made *runs* legible.
This one makes *file edits* legible, and removes the click tax that hides them.

## Problem

Shell activity summaries v2 solved viewport dominance by collapsing consecutive
completed tool calls into one activity row. It gave shell runs a rich summary
and left every other category as a bare count, so a file edit collapses to less
information than a `grep`:

```
Edited 1 · Ran git add · git commit · +3 more · Waited 4
```

`Edited 1` is the least informative possible rendering of the most consequential
event in the group. A reader cannot see that a file changed, which file, or how
much, without expanding the group and then the specific row.

Web can eventually show a real diff — `EditDiffView`
(`web/src/components/session-workspace/TimelinePane.tsx:422`) renders a
line-level diff with collapsed unchanged context and `+N −M` stats. Inside a
group it is two clicks away, and only one grouped child can be open at a time:
`expandedTools` is already a `Set<string>` (`TimelinePane.tsx:900`), but the
group render narrows it to a single matching child at `TimelinePane.tsx:1271`
and `ActivityChip` accepts only one `expandedInteractionKey`. iOS has no diff
rendering at all — no `old_string`/`new_string` handling exists anywhere under
`ios/Sources/`; `WebTranscriptView.swift` emits raw input/output into WebKit
`<details>` elements.

## Principle

Show what the agent **did**. Collapse what the agent **looked at**.

Reads, greps, `git status`, and polls are cheap to re-derive, and their content
describes the world rather than the agent's action. Missing one costs little.
File edits are the work product. A failed command's output is the third case:
not a mutation, but not re-derivable either, because that exact failure is gone.

This is a category axis, not a verbosity axis. `activityCategory()`
(`timelineModel.ts:155`) already classifies every interaction as one of
`search | read | list | view | edit | call | run | wait`. v2 built the
classifier and then spent all its presentation budget on `run`.

Note on ownership: the server-owned presentation `aggregate` only carries
`search | read | list | wait` (`toolTiers.generated.ts:5`; every edit tool in
`config/tool-tiers.json` has `aggregate: null`). Both clients infer the `edit`
category from tool names and labels via duplicated regexes
(`timelineModel.ts:159`, `TimelineBuilder.swift:166-185`). This spec keeps that
arrangement. Promoting `edit` into the server contract is a larger change than
the defect warrants and is explicitly out of scope.

## Approach

Grouping boundaries do not change. Collapse stays exactly as aggressive as it is
today. What changes is what a collapsed row is allowed to say about an edit, and
how many diffs a reader can hold open at once.

The rejected alternative here is worth naming, because it was the first design:
pulling edits out of mixed activity groups so each one gets its own row. It
fails on real transcripts. A refactor alternates `edit read edit read`, every
homogeneous run has length one, and the timeline degenerates to one row per
call — reintroducing viewport dominance from the other direction. Naming the
files in the collapsed summary achieves the same visibility with none of that
cost, and touches far less code.

## Rejected alternatives

**Verbosity modes as the fix.** A mode does not repair the default collapsed
representation; it asks the reader to opt out of a summary that should have been
informative in the first place. Out of scope here. A session-local "show
individual calls" inspection control is a legitimate future audit surface and is
not precluded by anything below — it would need no server column, migration, or
account preference. What stays rejected outright is rendering **unbounded**
outputs: one `make test-ci` log is 10k lines and dominates the transcript, which
is the exact failure v2 existed to fix.

**Command-importance ranking.** Rejected in v2, still rejected. Every rule below
keys off structural position, objective runtime state, or the existing category.

## Rules

### R1 — Collapsed summaries name edited files with stats

`formatActivitySummary()` stops rendering `Edited N`. It renders the files:

```
Edited timelineModel.ts +4 −1 · TimelinePane.tsx +40 −11 · +1 more
Read 6 · Ran git diff · make test
```

Files are **deduplicated by path** and shown as the **first two unique files in
first-seen order**, then `+N more`. This deliberately does not reuse the shell
first-plus-last bracket: chronology brackets a *sequence of operations*
meaningfully, but a file list is a set, and `A · A · +1 more` is a real outcome
of applying first-plus-last to `A, B, A`. First-two-unique is deterministic and
introduces no file-importance ranking.

Only the basename is shown. Full paths appear in the expanded diff header, which
already renders them.

### R2 — Standalone edit rows carry the stat in the collapsed header

An ungrouped edit row shows path and stat without any click:

```
Edited  timelineModel.ts  +4 −1
```

Header stats and diff body derive from one cached computation (R5), so they
cannot disagree.

### R3 — Edit-shape coverage

`EditDiffView` currently understands only `old_string`/`new_string`. That covers
one provider shape and silently fails the headline principle for the rest, so
coverage is required scope, not an open question:

| Input shape | Stat | Body |
| --- | --- | --- |
| `old_string` + `new_string` | line diff `+N −M` | existing `EditDiffView` |
| write/create with known content | `+N` | content as all-added |
| delete with known content | `−N` | content as all-removed |
| `apply_patch` with patch text | hunk-counted `+N −M` | patch text |
| anything else | no stat | existing raw-input fallback |

Unknown shapes fall back to the file name with no stat. Fail-closed, consistent
with v2's parser posture: never fabricate a stat.

### R4 — Failures show a bounded preview without a click

Failed and non-zero-exit interactions already stay out of activity groups
(`isActivityEligible`, `timelineModel.ts:141`). They now also render a preview
inline in the collapsed state, monospace and dimmed, below the header.

The preview is **2 head lines + 8 tail lines, capped at 4 KB**. Head matters
because a single-line megabyte JSON error or a stack trace whose exception
heading is at the top would be entirely lost to a pure tail.

One predicate drives all four behaviors — group exclusion, error styling, the
`exit N` chip, and this preview. Today `exit N` renders only when
`parseLonghouseOutput()` recognizes the wrapper, so a structured failure can be
excluded from grouping while still looking successful. Unifying that is part of
this rule.

Dropped and orphan results are deliberately *not* folded into that predicate.
They already carry their own chip and styling, and their recorded "output" is a
placeholder string rather than a command's error text.

### R5 — Multi-open grouped children, memoized diffs

`ActivityChip` accepts the expanded-key `Set` instead of a single
`expandedInteractionKey`, and `TimelinePane.tsx:1271` stops narrowing the set to
one child. Standalone rows are already multi-open; this brings grouped children
to parity. Expansion stays user-driven — **no auto-open-all**. A group of twenty
edits auto-opening its diffs would recreate viewport dominance directly.

Diff computation is memoized per interaction in a `WeakMap`, in the same shape
as the existing `shellSalienceCache` (`timelineModel.ts:65`), yielding the file
path, `{ added, removed }`, and the classified patch once, then reusing that for
both the header and the diff body.
`lineDiff()` (`web/src/lib/sessionWorkspace/diff.ts`) is O(n×m) in time and
memory and `formatActivitySummary()` runs from render, so an unmemoized
implementation would do quadratic work on every frame.

A size budget is checked **before** the LCS runs, not after: inputs exceeding
the cell budget skip diffing, report no stat, and render as raw input. Computing
a rendered-line count first would already have paid the cost the guard exists to
avoid.

### R6 — iOS parity

`ios/Sources/Shared/TimelineBuilder.swift` gains R1's summary construction and
must produce byte-identical summary strings to web against the shared fixture
corpus. `ios/Sources/LonghouseApp/WebTranscriptView.swift` gains the R2 header
stat, an R3-equivalent diff renderer, the R4 failure preview, and the `exit N`
chip it currently lacks entirely.

WebTranscriptView replaces retained nodes when a payload signature changes,
which can drop open `<details>` state during streaming. Expansion state must
survive a signature change for an unaffected row.

### R7 — Live and navigation behavior

- A pending edit is standalone; when its result arrives it may be absorbed into
  a group, changing row structure without increasing item count. The web
  scroll-follow logic (`TimelinePane.tsx:970-993`) only handles appended-item
  growth. Regrouping must not scroll-jump or drop focus.
- Grouped children currently share the parent's row ID
  (`timelineModel.ts:718-733`), so clicking a child toggles expansion without
  selecting it. With multi-open, each child gets a distinct row ID so focus,
  deep links, and keyboard navigation can address a specific diff.
- No virtualization exists on either client; the risk is scroll anchoring and
  DOM growth, not windowing. Not addressed here beyond not making it worse.

### R8 — Accessibility

- Group and child rows both expose `aria-expanded` and `aria-controls` against
  their own IDs.
- Added/removed conveyed with text, not color alone — the `+N −M` stat is
  literal text, and the diff header carries an `sr-only` "N lines added, M lines
  removed" restatement.
- Diff expansion and the "show earlier" affordance stay real `<button>`s, so
  they remain keyboard reachable.
- Failure previews are `role="note"` with an `sr-only` "Error output:" prefix.
  Deliberately not `role="alert"`: the preview is static row content, not a live
  region, and alerting on every historical failure in a transcript would be
  hostile to screen-reader users.
- Focus behavior when a standalone edit becomes grouped is pre-existing and
  untouched here.
- The auto-open question is moot under R5, so no conflict with the existing
  latest-eight overflow (`EXPLORATION_OVERFLOW_VISIBLE`) arises.

## Scope

Client-side only. Presentation contract, shell parser, server projection, and
stored evidence are unchanged. Historical sessions recompute at read time; no
migration.

## Verification

Extend `config/shell-activity-summary-fixtures.json` with cases locking:

- Edit inside a mixed group rendering named files with stats (R1).
- Duplicate file paths deduplicating rather than repeating (R1).
- Each R3 input shape, including unknown-shape fallback with no stat.

Behavioral coverage:

- Alternating `edit read edit read` transcript — asserts eight calls still
  collapse to one group, which is the whole reason grouping was preserved.
- Live regrouping (pending edit → absorbed into a group) is unchanged by this
  work and is covered by the existing grouping tests, not by a new one.
- Huge one-line failure output bounded by the 4 KB cap.
- Oversized diff input skipping LCS via the pre-computation budget.
- Two grouped diffs open simultaneously.

Suites: `make test-frontend` and `make test-ios`. Note that the iOS transcript
is WebKit-rendered HTML inside `WebTranscriptView`, not SwiftUI, so a `#Preview`
cannot snapshot a diff row; the payload golden
(`tests/fixtures/transcript-payload/hostile-transcript.golden.json`) plus
`EditSummaryTests` are the real guard there, and `render-previews.sh` only
confirms no SwiftUI regression.

Web visual QA runs against `scripts/ui-fixtures/sessionDetailStress.ts`, which
now carries a real `old_string`/`new_string` edit alongside the existing
`apply_patch` (unknown-shape fallback) and a non-zero-exit shell call, so one
capture exercises R1, R3's fallback, R4, and R5 together.
