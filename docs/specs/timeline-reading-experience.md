# Timeline Reading Experience

Status: workshopping (living spec — David is collecting issues as he sees them)
Surfaces: web timeline (`web/src/components/session-workspace/TimelinePane.tsx`,
`web/src/styles/session-workspace.css`), shared tier config
(`config/tool-tiers.json` → generated TS/Swift), iOS transcript.

## Problem

The timeline reads like a log dump, not a conversation. Two concrete failures
observed on hosted david010 at wide viewports:

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

### Classifier contract

Input: raw command string from the tool call (`command`/`cmd` field).
Output: `read` (demote) or `opaque` (keep action tier). Rules:

1. **Per-segment strictness.** Split on `&&`, `||`, `;`, `|`. Every segment
   must pass or the whole command is `opaque`. `cd` segments are neutral.
   Leading `VAR=value` assignments are stripped per segment.
2. **Any redirect (`>`, `>>`) anywhere → `opaque`.** Catches file writes
   via bash categorically. (Heredoc `<<` also → `opaque`.)
3. **Allowlist first words** (basename of segment head): `grep rg ls find
   cat head tail nl wc stat which file echo pwd du df ps env printenv
   whoami hostname date awk jq sqlite3 tree diff column sort uniq xxd
   basename dirname type true man`.
4. **Special cases:** `sed` is read unless `-i` present. `git` requires a
   read subcommand (`status log diff show branch remote rev-parse ls-files
   blame describe shortlog`). `ssh [flags] host "cmd"` unwraps one level
   and classifies the inner command recursively; unparseable ssh →
   `opaque`.
5. Command substitution `$(…)`, backticks, `for`/`while`, functions,
   subshells → `opaque` (no shell parsing heroics).

### Wiring

- Rules live in `config/tool-tiers.json` under a new `shell_classifier`
  block (allowlist, git subcommands, shell tool names). Structural rules
  (redirects, segmenting, ssh unwrap) are code in the generator templates —
  they are grammar, not data.
- `scripts/generate/tool_tiers.py` emits `classifyShellCommand()` in both
  `toolTiers.generated.ts` and `ToolTiers.generated.swift` so web and iOS
  stay byte-identical in behavior.
- Web call sites: `getToolTier` / `isExplorationEligible` /
  `formatExplorationSummary` in `timelineModel.ts` gain command awareness
  for tools listed as shell tools (`Bash`, `shell`, `exec_command`, …).
  Demoted shell reads get aggregate by head word: `grep|rg|find` → search,
  `ls|tree|du|df` → list, everything else → read.
- iOS call site: `TimelineBuilder` aggregate checks (lines ~82/97) pass the
  command string through the same generated Swift function.
- **Host chip:** an `ssh <host>` read does not change tier but surfaces the
  host in the row one-liner and in the exploration-chip summary (visibility
  badge, not salience escalation).
- MCP/unknown `default_tier` stays `action` for now — separate decision,
  not bundled into this change.

### Validation

- TS unit tests: classifier table tests (corpus-derived fixtures incl.
  ssh-wrapped reads, pipes, redirects, `sed -i`, git subcommands) plus a
  `timelineModel` grouping test proving consecutive shell reads collapse
  and a mutation breaks the run.
- Visual: `make ui-capture` fixture pass showing a Bash-heavy transcript
  collapsing (extend session-detail-stress with a shell exploration run).
- iOS: generated Swift compiles; TimelineBuilder unit test with a shell
  read run. Full suite at ship cutover, not per-phase.

## Change C — Grouping presentation

With B in place the existing NoiseChip collapse starts working on Bash-heavy
sessions. Refine:

- Collapsed run summary should say what happened, not just count:
  "Explored g55 WIS catalog · 9 commands · 8.1s" (paths/hosts distilled).
- Runs stay one-click expandable; expanded items keep per-command timing and
  the existing detail drawer.
- Solo noise commands render as the existing demoted one-liner.

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
- [ ] Bash exploration spam crowding prose (Changes B+C)
- [x] Draft reply row + review copy removed (web) — backend/iOS removal pending
- [x] Stop shown while idle (web: now working-only)
- [x] Jammed meta separators in runtime strip (web CSS)
- [ ] Composer color scheme hard to read (Change D)

## Non-goals

- No per-user configurable tiers; one opinionated default.
- No hiding of any tool call — everything remains reachable via expand.
