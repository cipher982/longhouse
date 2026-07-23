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
- [x] Bash exploration spam crowding prose (Change B v1 implemented: classifier
      + fixtures + web/iOS wiring; Sol+Grok review synthesized — ssh unwrap
      and host chip deferred to v2, summary refinement remains under C)
- [x] Draft reply row + review copy removed (web) — backend/iOS removal pending
- [x] Stop shown while idle (web: now working-only)
- [x] Jammed meta separators in runtime strip (web CSS)
- [ ] Composer color scheme hard to read (Change D)

## Non-goals

- No per-user configurable tiers; one opinionated default.
- No hiding of any tool call — everything remains reachable via expand.
