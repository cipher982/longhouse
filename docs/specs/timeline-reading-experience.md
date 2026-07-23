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

## Change B — Content-aware Bash salience (classifier)

Sub-classify `Bash`/`shell`/`exec` interactions by command string:

- **Read-only commands → noise/context**, eligible to join exploration runs:
  `grep`, `rg`, `ls`, `find`, `cat`, `head`, `tail`, `wc`, `stat`, `which`,
  `echo`, `pwd`, `git status/log/diff/show`, `sqlite3 … ".tables"/SELECT`, etc.
  Unwrap one level of `ssh <host> "<cmd>"` and classify the inner command;
  pipes classify by the most-consequential segment.
- **Mutating/heavyweight → action** (unchanged full row): `rm`, `mv`, `cp`
  into tracked paths, `git commit/push/reset`, `docker`, `kill`, installs,
  redirects (`>`), `make`, deploys, long-running anything.
- **Unknown/ambiguous → context** (middle), not action. Also revisit
  `default_tier: action` for MCP/unknown tools → `context`.
- Remote target (`ssh cube …`) does not change tier but adds a host chip to
  the row/summary — visibility badge, not salience escalation.

Classifier lives beside the tier config so web and iOS share it (extend the
`tool_tiers.py` generator model with command-pattern rules).

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

- [ ] Horizontal ping-pong at wide viewports (Change A)
- [ ] Bash exploration spam crowding prose (Changes B+C)
- [x] Draft reply row + review copy removed (web) — backend/iOS removal pending
- [x] Stop shown while idle (web: now working-only)
- [x] Jammed meta separators in runtime strip (web CSS)
- [ ] Composer color scheme hard to read (Change D)

## Non-goals

- No per-user configurable tiers; one opinionated default.
- No hiding of any tool call — everything remains reachable via expand.
