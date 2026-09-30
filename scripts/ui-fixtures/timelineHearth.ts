/**
 * Timeline fixture for the Hearth status fire: one row per fire state
 * (working, very busy with subagents, waiting on you, idle a few minutes,
 * idle for hours, ended). The capture's stream mock replays
 * `buildTimelineHearthStreamBatch(n)` on every EventSource reconnect, so the
 * busy row keeps parent totals fixed while linked child archive counts advance;
 *
 * Clock: the capture freezes Date.now at 2026-04-15T16:12:00Z.
 */

import { makeTimelineCard } from "./timelineCardStress";

type Card = ReturnType<typeof makeTimelineCard>;

const NOW = "2026-04-15T16:12:00Z";
const minutesBefore = (m: number) => new Date(Date.parse(NOW) - m * 60_000).toISOString();
const minutesAfter = (m: number) => new Date(Date.parse(NOW) + m * 60_000).toISOString();

function live(tool: string | null) {
  return {
    status: "working",
    presence_state: tool ? "running" : "thinking",
    presence_tool: tool,
    active_tool: tool,
    presence_updated_at: minutesBefore(0.05),
    last_live_at: minutesBefore(0.05),
    runtime_source: "managed_local_transport",
    confidence: "live",
    capabilities: { live_control_available: true, host_reattach_available: true, reply_to_live_session_available: true },
    control: { managed_transport: "claude_channel", source_runner_id: null, source_runner_name: null, attach_command: null },
  };
}

function withState(card: Card, patch: Record<string, unknown>): Card {
  return { ...card, head: { ...card.head, session_state: { ...card.head.session_state, ...patch } } } as Card;
}

/** Counts that grow with each stream batch, so the fires see deltas. */
function counts(n: number) {
  return {
    working: {
      tool_calls: 38 + Math.floor(n / 2),
      assistant_messages: 20 + Math.floor(n / 3),
      user_messages: 4 + (n > 0 && n % 5 === 0 ? 1 : 0),
    },
    busyParent: { tool_calls: 1480, assistant_messages: 610, user_messages: 9 },
    busyChild: {
      alpha: { tool_calls: 34 + n * 3, assistant_messages: 12 + n, user_messages: 2 },
      beta: { tool_calls: 19 + n * 2, assistant_messages: 8 + n, user_messages: 1 },
    },
  };
}

function buildCards(n: number): Card[] {
  const c = counts(n);
  const working = withState(
    makeTimelineCard({
      id: "hearth-working",
      thread_root_session_id: "hearth-working",
      thread_head_session_id: "hearth-working",
      provider: "claude",
      project: "zerg",
      started_at: minutesBefore(35),
      last_activity_at: minutesBefore(0.05),
      timeline_anchor_at: minutesBefore(0.05),
      ...c.working,
      summary_title: "Hearth: port the fire into timeline rows",
      anchor_title: "Hearth: port the fire into timeline rows",
      timeline_title: "Hearth: port the fire into timeline rows",
      summary: "Reading the renderer and wiring one shared canvas behind the list.",
      first_user_message: "Reading the renderer and wiring one shared canvas behind the list.",
      origin_label: "cinder",
      ...live("Read"),
    } as never),
    { working_set: "open" },
  );
  const busy = withState(
    makeTimelineCard({
      id: "hearth-busy",
      thread_root_session_id: "hearth-busy",
      thread_head_session_id: "hearth-busy",
      provider: "codex",
      project: "zerg",
      started_at: minutesBefore(62),
      last_activity_at: minutesBefore(0.02),
      timeline_anchor_at: minutesBefore(0.02),
      ...c.busyParent,
      summary_title: "Catalog backfill: replay 40k transcripts",
      anchor_title: "Catalog backfill: replay 40k transcripts",
      timeline_title: "Catalog backfill: replay 40k transcripts",
      summary: "Fanning the replay across three workers and checking each shard.",
      first_user_message: "Fanning the replay across three workers and checking each shard.",
      origin_label: "cube",
      ...live("Bash"),
    } as never),
    {
      working_set: "open",
      activity: {
        state: "quiescent",
        tool: null,
        observed_at: minutesBefore(3),
        valid_until: minutesBefore(1),
      },
      delegation: {
        state: "pending",
        count: 4,
        kinds: { subagent: 2, shell: 1, monitor: 1 },
        source: "claude_hook",
        observed_at: minutesBefore(0.02),
        valid_until: minutesAfter(2),
        items: [
          {
            id: "task-alpha",
            kind: "subagent",
            status: "running",
            description: "Replay shard alpha",
            first_observed_at: minutesBefore(8),
            started_at: minutesBefore(7),
            last_activity_at: minutesBefore(0.04),
            session_id: "hearth-child-alpha",
            ...c.busyChild.alpha,
          },
          {
            id: "task-beta",
            kind: "subagent",
            status: "running",
            description: "Replay shard beta",
            first_observed_at: minutesBefore(7),
            started_at: minutesBefore(6),
            last_activity_at: minutesBefore(0.06),
            session_id: "hearth-child-beta",
            ...c.busyChild.beta,
          },
          {
            id: "task-command",
            kind: "shell",
            status: "running",
            description: "Check replay logs",
            last_activity_at: minutesBefore(0.5),
            session_id: null,
            tool_calls: null,
          },
          {
            id: "task-monitor",
            kind: "monitor",
            status: "running",
            description: "Watch replay health",
            last_activity_at: minutesBefore(0.5),
            session_id: null,
            tool_calls: null,
          },
        ],
      },
      presentation: {
        primary: {
          key: "delegated_work",
          label: "Background · 2 agents · 1 command · 1 monitor",
          tone: "active",
          observed_at: minutesBefore(0.02),
        },
      },
    },
  );
  const waitingBase = makeTimelineCard({
    id: "hearth-waiting",
    thread_root_session_id: "hearth-waiting",
    thread_head_session_id: "hearth-waiting",
    provider: "claude",
    project: "zerg",
    started_at: minutesBefore(50),
    last_activity_at: minutesBefore(4),
    timeline_anchor_at: minutesBefore(4),
    tool_calls: 120,
    assistant_messages: 60,
    user_messages: 6,
    summary_title: "Rotate the canary signing key",
    anchor_title: "Rotate the canary signing key",
    timeline_title: "Rotate the canary signing key",
    summary: "Asked before replacing the key in Infisical.",
    first_user_message: "Asked before replacing the key in Infisical.",
    origin_label: "cinder",
    ...live(null),
    presence_state: "needs_user",
  } as never);
  const waiting = withState(waitingBase, {
    working_set: "open",
    activity: { ...waitingBase.head.session_state.activity, state: "quiescent" },
    pending_interaction: { id: "hearth-approval", kind: "approval", opened_at: minutesBefore(4), can_respond: true },
    presentation: {
      ...waitingBase.head.session_state.presentation,
      primary: { key: "needs_approval", label: "Needs approval", tone: "blocked", observed_at: minutesBefore(4) },
    },
  });
  const idleCard = (id: string, minutes: number, title: string, provider: string) =>
    withState(
      makeTimelineCard({
        id,
        thread_root_session_id: id,
        thread_head_session_id: id,
        provider,
        project: "longhouse-mobile",
        started_at: minutesBefore(minutes + 45),
        last_activity_at: minutesBefore(minutes),
        timeline_anchor_at: minutesBefore(minutes),
        tool_calls: 64,
        assistant_messages: 30,
        user_messages: 5,
        summary_title: title,
        anchor_title: title,
        timeline_title: title,
        summary: "Finished and waiting for the next prompt.",
        first_user_message: "Finished and waiting for the next prompt.",
        origin_label: "cinder",
        ...live(null),
        presence_state: "idle",
        presence_updated_at: minutesBefore(minutes),
      } as never),
      { working_set: minutes < 60 ? "open" : "history" },
    );
  const idleRecent = idleCard("hearth-idle-recent", 3, "Tighten the iOS reconnect banner", "claude");
  const idleWarm = idleCard("hearth-idle-warm", 24, "Review the storage lifecycle spec", "codex");
  const idleHours = idleCard("hearth-idle-hours", 170, "Profile the transcript scroll", "claude");
  const ended = makeTimelineCard({
    id: "hearth-ended",
    thread_root_session_id: "hearth-ended",
    thread_head_session_id: "hearth-ended",
    provider: "codex",
    project: "longhouse-mobile",
    started_at: minutesBefore(300),
    ended_at: minutesBefore(240),
    last_activity_at: minutesBefore(240),
    timeline_anchor_at: minutesBefore(240),
    tool_calls: 22,
    assistant_messages: 9,
    user_messages: 3,
    summary_title: "Archive import cleanup",
    anchor_title: "Archive import cleanup",
    timeline_title: "Archive import cleanup",
    summary: "Closed after the history import verified.",
    first_user_message: "Closed after the history import verified.",
    terminal_state: "user_closed",
    status: "completed",
    origin_label: "cinder",
  } as never);
  return [busy, working, waiting, idleRecent, idleWarm, idleHours, ended];
}

export function buildTimelineHearthFixture() {
  const sessions = buildCards(0);
  return {
    sessions: { sessions, total: sessions.length, has_real_sessions: true },
    filters: { projects: ["zerg", "longhouse-mobile"], providers: ["claude", "codex"], machines: ["cinder", "cube"] },
    runners: { runners: [] as [] },
  };
}

/** One SSE body: the cards as they stand after `n` stream batches. */
export function buildTimelineHearthStreamBatch(n: number): string {
  const cards = buildCards(n);
  const lines = [`retry: 700`, `event: connected`, `data: ${JSON.stringify({ message: "fixture", stream_epoch: "hearth-fixture" })}`, ""];
  for (const card of cards.slice(0, 2)) {
    lines.push(`event: session_upsert`, `data: ${JSON.stringify({ session: card, total: cards.length, has_real_sessions: true })}`, "");
  }
  return lines.join("\n") + "\n";
}
