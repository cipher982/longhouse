# Timeline Reading Experience

Status: workshopping (living spec)
Surfaces: web timeline (`web/src/components/session-workspace/TimelinePane.tsx`,
`web/src/styles/session-workspace.css`), shared tier config
(`config/tool-tiers.json` → generated TS/Swift), iOS transcript.

## Problem

The timeline reads like a log dump, not a conversation. Two concrete failures
observed on a hosted instance at wide viewports:

1. **Horizontal ping-pong.** The conversation column has no overall max-width —
   only viewport padding. User messages pin `flex-end`, assistant `flex-start`,
   so on a ~2000px window the eye travels ~1300px of dead space between turns.
   The messages barely overlap horizontally. (ChatGPT reference: one ~768px
   centered column; alignment happens *inside* it.)
2. **Tool rows crowd out prose.** Ten consecutive read-only `Bash` rows
   (`grep`, `ls`, `find`, `ssh cube "ls …"`) each render as full-width action
   rows. The real AI messages — the big picture — drown. The existing tier
   system (`noise`/`context`/`action` + NoiseChip exploration-run collapse)
   never fires because `Bash` is hard-coded `tier: action` and action rows
   break exploration runs. Tool-name classification misses that the signal
   lives in the command content. `default_tier: action` similarly inflates
   unknown/MCP tools.

## Principles

- The AI/user prose is the document; tool calls are supporting evidence.
  Never fully hide tools, but salience must follow consequence.
- Salience axis is **read vs. mutate**, not local vs. remote or tool name.
  A `grep` is noise whether it runs locally, through Bash, or over ssh.
  An `Edit`, `rm`, `git push`, or state-changing remote command is action.
- One classification source of truth (`config/tool-tiers.json` + generator)
  serving web and iOS identically.

## Change A — Centered reading column (layout)

- Give the timeline list one centered column (~82ch) via `max-width` +
  `margin-inline: auto` on the flex parent.
- Assistant messages fill the column (they're the essay); user messages are
  compact right-aligned within the column, max ~70%, with a faint tint/radius
  so the offset doesn't read as ragged-right accident.
- Tool rows keep a wider band (`--tl-tool-max`) but share the same centerline —
  a deliberate two-tier width system (prose narrow, terminal content wider).

Open question: user bubble = faint background tint (leaning yes) vs. pure
chromeless offset.

## Change B — Content-aware shell salience (classifier)

### Evidence (local corpus, 5,570 real shell commands)

First-word head of the distribution: `sed` 16.2%, `git` 15.1%, `grep`+`rg`
13.3%, `uv` 5.6%, `nl` 4.7%, `ls` 3.3%, `find` 3.3%, `ssh` 3.0%. With the
conservative classifier below, **40.4% of all shell commands classify as
read-only**, and 46% of those occur in adjacent runs of 2+ (371 runs, avg
2.8, max 10). So ~20% of shell rows collapse into exploration chips and
another ~20% demote to quiet one-liners.

### Design principle: detect boring, never detect danger

Enumerating dangerous commands is an unbounded long tail. Instead: a
conservative **allowlist of read-only commands demotes; everything
unrecognized keeps today's full action row.** Failure asymmetry does the
work — a missed read renders big (status quo, harmless); a mutation can
only demote if it passes an explicit read-only shape, and mutating verbs
are simply not on the list. Salience is not safety: a demoted row is still
visible, expandable, and grouped, so the classifier only needs to be
roughly right, forever. No pressure to chase freak cases.

### Classifier contract (v1, hardened per Sol + Grok review)

Input: raw command string from the tool call (`command` then `cmd` field;
missing or non-string → `opaque`). Output: `read` (demote) or `opaque`
(keep action tier). **Fail closed at every rule** — anything the grammar
does not affirmatively recognize is `opaque`.

1. **Opaque-on-sight structures.** Any of: newline in the command, `&`
   (backgrounding), `|&`, write redirections in all spellings (`>`, `>>`,
   `>|`, `&>`, `&>>`, `n>`, `n>>`, `n>&m`), heredoc `<<`/herestring `<<<`,
   process substitution `<(…)`/`>(…)`, command substitution `$(…)` or
   backticks, `for`/`while`/`if`/function definitions, subshell `(…)`,
   unbalanced quotes.
2. **Per-segment strictness.** Split on `&&`, `||`, `;`, `|`. Every
   segment must pass or the whole command is `opaque`. `cd` segments are
   neutral, but a command that is *only* `cd`/empty segments is `opaque`.
   Leading `VAR=value` assignments are stripped per segment.
3. **Allowlist first words — bare names only, no paths** (`/tmp/ls` is not
   `ls`): `grep rg ls cat head tail nl wc stat which echo pwd du df ps
   printenv whoami pwd tree diff column uniq jq basename dirname type
   true man`. Dropped from the draft after review: `awk` (system()),
   `env` (runs argv), `sqlite3` (writes), `find` (-delete/-exec), `sort`
   (-o), `date` (-s), `hostname` (sets), `xxd` (-r writes), `file` (-C).
4. **Special cases:**
   - `sed`: read only in explicit print shape — `-n` present, no
     `-i`/`--in-place` in any spelling, script matches print-only
     (`p`/`=`-terminated address script, no `w`/`W`/`e`/`s///w`).
   - `git`: skip global options (`-C x`, `-c k=v`, `--git-dir=…`), then
     require subcommand in `status log diff show rev-parse ls-files blame
     describe shortlog`. `branch`/`remote` are NOT read (mutate via
     flags).
   - `ssh`: **postponed to v2.** ssh is always `opaque` in v1 — ssh
     options (`-o ProxyCommand=…`, `-F`) can execute local commands, and
     safe unwrapping needs its own review. The host chip UI defers with
     it.
5. Aggregate for demoted reads by head word: `grep|rg` → search,
   `ls|tree|du|df` → list, everything else → read.

Additional demotion gates (beyond command text):

- Only completed interactions demote; pending/running/orphan stay action.
- Nonzero exit (when parseable from output) → stays action. Errors must
  never disappear into an "Explored" chip.

### Wiring

- `config/tool-tiers.json` gains a `shell_classifier` block: shell tool
  names (`Bash`, `shell`, `shell_command`, `exec_command`,
  `run_shell_command` — explicitly excluding `write_stdin`), the
  read-only allowlist, git read subcommands, and aggregate head-word
  mapping. **The generator emits constants only.** The grammar is
  handwritten twice — `web/src/lib/sessionWorkspace/shellSalience.ts` and
  `ios/Sources/Shared/ShellSalience.swift` — because a shell grammar in
  Python f-string templates would be untestable and drift-prone.
- **Parity is enforced by a shared conformance corpus**,
  `config/shell-salience-fixtures.json`: read cases from the real corpus
  plus an adversarial must-stay-opaque set (find -delete, sed -i.bak,
  git branch -D, env rm, sqlite3 writes, sort -o, every redirect
  spelling, multiline, lone `&`, process substitution, spoofed paths).
  Web vitest and iOS XCTest both run the full corpus; any false demotion
  fails CI.
- One resolver, `resolveShellSalience(toolName, command) → {tier,
  aggregate} | null`, layered over the untouched name-based tables (JSON
  keeps Bash as `action` so unrecognized commands need no fallback
  logic). Web call sites are exactly the three grouping/render gates:
  `getToolTier`, `isExplorationEligible`, `formatExplorationSummary`.
  iOS call site: `TimelineBuilder`'s aggregate checks.
- MCP tools already default to `noise` tier without aggregation
  (`mcp_default_tier: noise`); only unknown non-MCP tools default to
  `action`. Unchanged here.

### Expected impact (re-measured with hardened v1 rules)

Re-run on the 5,570-command corpus with the hardened v1 rules: **32.7%
demote** (1,823 commands), 856 of them in adjacent runs of 2+ (313 runs,
avg 2.7, max 10). The review-driven tightening cost ~8 points versus the
draft — mostly `ssh`, `find`, and non-print `sed` — while keeping the
g55-style `grep`/`ls`/`cat` runs fully covered.

### Rejected alternatives (do not revisit casually)

- Collapse-all-Bash: hides mutations; breaks the salience principle.
- Danger denylist: unbounded long tail; the design detects boring, never
  danger.
- Output-length or duration heuristics: unstable, provider-dependent.
- Generating the parser from config templates: untestable, drifts.

### Validation

- TS unit tests: classifier table tests (corpus-derived fixtures incl.
  ssh-wrapped commands, pipes, redirects, `sed -i`, git subcommands) plus a
  `timelineModel` grouping test proving consecutive shell reads collapse
  and a mutation breaks the run.
- Visual: `make ui-capture` fixture pass showing a Bash-heavy transcript
  collapsing (extend session-detail-stress with a shell exploration run).
- iOS: generated Swift compiles; TimelineBuilder unit test with a shell
  read run. Full suite at ship cutover, not per-phase.

## Change C — Grouping presentation

Superseded by Change E. Shell salience remains useful for singleton rows, but
there is one grouping model: activity runs.

With B in place the existing NoiseChip collapse starts working on Bash-heavy
sessions. Refine:

- Collapsed run summary should say what happened, not just count:
  "Explored g55 WIS catalog · 9 commands · 8.1s" (paths/hosts distilled).
- Runs stay one-click expandable; expanded items keep per-command timing and
  the existing detail drawer.
- Solo noise commands render as the existing demoted one-liner.

## Change E — Prose-first activity runs

### Evidence from the hosted corpus

A 2026-07-23 sample ranked real sessions by tool volume, then inspected the
first 200 loaded entries from eight sessions across Codex, Claude, Cursor, and
OpenCode. The worst sessions contain 3,044, 2,119, 1,549, and 1,443 tool calls.
Even after semantic translation and exploration grouping, individual tool rows
still occupied 58–85% of visible timeline rows:

| Provider / workload | Visible rows | Individual tools | Existing groups |
|---|---:|---:|---:|
| Codex / remove launch feature | 95 | 74 | 8 |
| Codex / disk investigation | 92 | 74 | 7 |
| Codex / metadata refinement | 107 | 81 | 3 |
| Claude / RL work | 120 | 67 | 4 |
| Cursor / patent work | 57 | 43 | 6 |
| OpenCode / Hatch work | 30 | 25 | 1 |

The translation layer is working, but the resulting page is still a ledger.
The failure is the grouping boundary: an `Edit`, shell validation, plan update,
web call, or provider wrapper ends an exploration group even when all of those
calls support one uninterrupted assistant turn.

### Product contract

The default timeline is a conversation, not an execution trace.

- Every consecutive run of **two or more completed tool calls with no known
  failure signal**
  between prose, user messages, actions, or branch seams becomes one compact
  activity row.
- The row summarizes consequences across the whole run, for example:
  `Searched 4 · Read 6 · Edited 2 · Ran 3`.
- Expanding the row reveals every original call in order, with the existing
  semantic label, input summary, duration, result, and raw provider payload.
- Single completed calls keep their current semantic row. Short turns should
  not acquire an unnecessary wrapper.
- Running, pending, dropped, orphaned, and known-failed calls never join a group.
  They remain full rows so live state and problems cannot disappear.
- Human-interaction calls (`AskUserQuestion`, `request_user_input`, and
  approval/permission tools) never join a group. Questions remain first-class
  transcript content.
- Provider identity does not affect grouping. Claude `tool_use`, Codex wrapped
  calls, Cursor calls, OpenCode calls, and future providers all enter through
  the same normalized interaction model.

### Deterministic summary vocabulary

Use the closed vocabulary from `tool-translation-experience.md` and normalized
presentation/aggregate metadata, not provider-specific raw names:

- search aggregate → `Searched`
- read aggregate → `Read`
- list aggregate → `Listed`
- wait aggregate → `Waited`
- edit/write/create/patch labels or names → `Edited`
- web search/fetch/browser tools → `Viewed`
- agent/task/subagent/MCP calls → `Called`
- everything else → `Ran`

Order is stable: Searched, Read, Listed, Viewed, Edited, Called, Ran, Waited.
Omit zeroes. The count badge remains the exact number of calls.
This is intentionally deterministic: it is fast, replayable over historical
data, identical on web and iOS, and cannot invent activity.

### Implementation and acceptance gates

- Replace exploration-only grouping with activity-run grouping in the shared
  web and iOS projections. Delete the old eligibility boundary and old
  `noise_group` / `passiveGroup` naming rather than maintaining two models.
- A known failure is a failed/error call state, nonzero parsed exit, explicit
  provider tool-error marker, or structured result with an explicit false
  success/ok value. Unknown result semantics remain completed evidence; the UI
  does not claim success, and expansion exposes the exact result.
- Preserve selection/deep-link behavior: selecting any child call opens its
  parent group and the exact call.
- Keep the full raw event stream untouched; this is a presentation projection.
- Add parity fixtures covering mixed reads, shell calls, edits, plans, waits,
  questions, failures, pending calls, prose boundaries, and provider wrappers.
- Re-run the same hosted sample after implementation, collapsed by default and
  using the same first-200-entry slice. Individual tool rows below 20% are a
  diagnostic target for completed historical sessions, not a reason to hide
  live or failed work. The total expandable call count must not change.
- Group identity is anchored to the first call. A pending call stays separate;
  completed calls before and after it form stable groups instead of reshaping a
  prior group when live state changes.
- Visually inspect at least one worst-case Codex session plus Claude, Cursor,
  and OpenCode examples on the real timeline at desktop width. Prose should be
  the dominant visual rhythm; activity rows should be short, quiet, and
  expandable.

### 2026-07-23 replay result

The same first-200-entry slices were replayed through the new projection. The
largest examples fell from 95 → 25 visible rows (Codex, 3,044-tool session),
92 → 19 (Codex, 2,119 tools), 107 → 39 (Codex, 1,443 tools), 57 → 13
(Cursor), and 30 → 8 (OpenCode). Individual tool rows are now 0–13% in those
samples. The Claude slide workshop remains the natural edge case at 19
singleton tools among 87 rows because Claude narrates between nearly every
edit; those calls are real conversational boundaries, so they remain visible.

## Change D — Composer cleanup

- **Draft reply is dead.** The "Draft reply" button + "Review the suggestion
  before sending." copy came from the old AI-drafts-a-response feature.
  Removed from web (SessionChat). Still to remove: backend
  `/sessions/{id}/draft-reply` + `/api/agents/.../draft-reply` endpoints and
  `_build_managed_local_draft_reply_response` in `session_chat_impl.py`; iOS
  Draft reply button + `draftReply` viewmodel path + generated API surface.
- **One primary action at a time.** Stop no longer renders while the session
  is idle (web: gated on an actually-running turn). While working, the
  ChatGPT-style morph doesn't map 1:1 — Longhouse legitimately offers
  Send update (steer) / Queue next mid-turn — but idle should show Send
  only, working should show Stop + one primary send intent, never a
  permanent Stop+Send pair.
- **Meta line legibility.** "Live control•Updated 8m ago•Session 13:50" was
  jammed because inline-flex drops whitespace text nodes around the bullet;
  separator now has real margins. Consider whether Session HH:MM earns its
  spot at all.
- **Color audit.** Composer reads as yellow/red/brown soup: red Stop, gold
  Send, brown field on brown card. Reserve red strictly for Stop-while-
  running and errors; Send should be the single high-contrast accent; drop
  warning-toned borders on the idle composer. Fold into a broader
  timeline-palette pass (tool-row ambers have the same problem).

## Running list (add as David spots things)

- [x] Horizontal ping-pong at wide viewports (Change A — shipped web CSS, 723dc9416)
- [x] Bash exploration spam crowding prose (Change B v1 implemented: classifier
      + fixtures + web/iOS wiring; Sol+Grok review synthesized — ssh unwrap
      and host chip deferred to v2, summary refinement remains under C)
- [x] Mixed tool runs still crowding prose (Change E: one activity grouping
      model across web/iOS, corpus replayed across four providers)
- [x] Draft reply row + review copy removed (web) — backend/iOS removal pending
- [x] Stop shown while idle (web: now working-only)
- [x] Jammed meta separators in runtime strip (web CSS)
- [ ] Composer color scheme hard to read (Change D)

## Non-goals

- No per-user configurable tiers; one opinionated default.
- No hiding of any tool call — everything remains reachable via expand.
