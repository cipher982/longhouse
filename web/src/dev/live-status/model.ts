import type {
  AgentEvent,
  AgentSessionProjectionItem,
} from "../../services/api/agents";
import type {
  ReceiptMark,
  ReplayFrame,
  RibbonState,
  Scene,
  SessionCapture,
} from "./types";

export const REPLAY_DURATION_MS = 30_000;
export const SCENES: Array<{ id: Scene; label: string; description: string }> =
  [
    {
      id: "recorded",
      label: "Recorded snapshot",
      description:
        "Unmodified recorded session facts. Nothing here claims a live connection.",
    },
    {
      id: "working",
      label: "Work arriving",
      description:
        "Recorded transcript events, delivered at a controlled cadence. Fresh work evidence and server receipts are separate.",
    },
    {
      id: "quiet",
      label: "Working, no output",
      description:
        "No new content, but provider activity evidence remains valid and the viewer receives heartbeats.",
    },
    {
      id: "expiry",
      label: "Evidence expires",
      description:
        "Work stops arriving. At 12 seconds activity authority expires while server heartbeats continue.",
    },
    {
      id: "reconnect",
      label: "Disconnect and replay",
      description:
        "Disconnect at 5s; reconnect at 12s. Old content arrives until the snapshot is applied at 18s. Replay is not live work.",
    },
    {
      id: "machine",
      label: "Machine unreachable",
      description:
        "At 8s a simulated fresh host observation reports the machine unreachable. The viewer connection stays healthy.",
    },
    {
      id: "return",
      label: "Return to the app",
      description:
        "Cached content remains visible. Reconcile at 5s; fresh provider evidence only arrives at 12s.",
    },
    {
      id: "attention",
      label: "Needs your attention",
      description:
        "At 6s a simulated explicit permission request replaces work motion with a steady attention state.",
    },
    {
      id: "finished",
      label: "Turn finishes",
      description:
        "At 10s a simulated terminal turn fact ends work animation. It does not imply a successful outcome.",
    },
  ];

export function parseCapture(text: string): SessionCapture {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== "object")
    throw new Error("Choose a Longhouse session capture JSON file.");
  const capture = value as Partial<SessionCapture>;
  if (
    capture.schema !== "longhouse.live-status-capture.v1" ||
    !capture.workspace?.session?.id ||
    !capture.workspace.session.session_state ||
    !Array.isArray(capture.workspace.projection?.items) ||
    !capture.workspace.session.session_state.presentation ||
    !capture.workspace.session.session_state.activity ||
    !capture.workspace.session.session_state.host ||
    !capture.workspace.session.session_state.transcript ||
    !capture.source?.sessionId ||
    !capture.capturedAt ||
    !Number.isFinite(Date.parse(capture.capturedAt))
  ) {
    throw new Error(
      "Not a valid live-status capture. Use make capture-live-status SESSION=<id>.",
    );
  }
  return capture as SessionCapture;
}

const RECEIPTS = [
  650, 1300, 2400, 3200, 4400, 5200, 6400, 7700, 8100, 9300, 11200, 13300,
  15000, 17400, 20000, 23200, 26400,
];
interface PreparedCapture {
  items: AgentSessionProjectionItem[];
  baseEnd: number;
  command: string | null;
  tool: string | null;
  host: string;
  stages: Map<string, AgentSessionProjectionItem[]>;
}
const prepared = new WeakMap<SessionCapture, PreparedCapture>();

function toolDetail(event: AgentEvent | null | undefined): string | null {
  if (!event) return null;
  const input = event.tool_input_json;
  if (typeof input === "string") return input;
  if (input && typeof input === "object" && !Array.isArray(input)) {
    const fields = input as Record<string, unknown>;
    for (const key of [
      "command",
      "cmd",
      "file_path",
      "path",
      "pattern",
      "query",
      "url",
    ]) {
      if (typeof fields[key] === "string" && fields[key]) return fields[key];
    }
  }
  return null;
}

function prepare(capture: SessionCapture): PreparedCapture {
  const cached = prepared.get(capture);
  if (cached) return cached;
  const items = capture.workspace.projection.items;
  // Keep genuine context and reveal the final tool sequence; never rewrite prose
  // or manufacture a tool result. Arrival timing alone is the design simulation.
  let lastReply = -1;
  for (let index = items.length - 1; index >= 0; index--) {
    const event = items[index].event;
    if (event?.role === "assistant" && !event.tool_name && event.content_text) {
      lastReply = index;
      break;
    }
  }
  const baseEnd = Math.max(1, (lastReply >= 0 ? lastReply : items.length) - 12);
  const toolEvent = items
    .slice(baseEnd)
    .find(
      (item) => item.event?.role === "assistant" && item.event.tool_name,
    )?.event;
  const result: PreparedCapture = {
    items,
    baseEnd,
    command: toolDetail(toolEvent),
    tool: toolEvent?.tool_name ?? null,
    host:
      capture.workspace.session.device_id ||
      capture.workspace.session.home_label ||
      "the machine",
    stages: new Map<string, AgentSessionProjectionItem[]>(),
  };
  prepared.set(capture, result);
  return result;
}

function clockLabel(iso: string | null | undefined): string {
  if (!iso || !Number.isFinite(Date.parse(iso))) return "Not observed";
  return new Date(iso).toISOString().slice(11, 19) + " UTC";
}

/** Pure seekable design projection. Synthetic facts never mutate the recorded capture. */
export function buildReplayFrame(
  capture: SessionCapture,
  scene: Scene,
  timeMs: number,
): ReplayFrame {
  const time = Math.min(REPLAY_DURATION_MS, Math.max(0, timeMs));
  const data = prepare(capture);
  const actual = capture.workspace.session.session_state;
  const description =
    SCENES.find((item) => item.id === scene)?.description ??
    SCENES[0].description;
  const action = data.tool ? `Using ${data.tool}` : "Working";
  const availableReceipts = RECEIPTS.slice(
    0,
    Math.max(0, data.items.length - data.baseEnd),
  );
  let arrivals = availableReceipts.filter((at) => at <= time);
  let tone: RibbonState["tone"] = "working";
  let headline = action;
  let detail = data.command;
  let detailKind: RibbonState["detailKind"] = "literal";
  let connection: RibbonState["connection"] = "connected";
  let observation = "Connected";
  let animateWork = true;
  let heartbeatAgeMs: number | null = time % 5000;
  let providerFact =
    "Simulated fresh provider activity; validity renewed independently of heartbeats";
  let hostFact = `Simulated fresh observation: ${data.host} reachable`;
  let transcriptFact =
    "Recorded content, controlled receipt timing; not proof of complete source coverage";

  if (scene === "recorded") {
    return {
      items: data.items,
      explanation: description,
      sourceLabel: "Recorded snapshot · no live connection",
      notice: null,
      ribbon: {
        tone: "quiet",
        headline:
          actual.presentation.primary?.label ?? "Recorded activity unknown",
        detail: `Captured ${clockLabel(capture.capturedAt)}`,
        detailKind: "explanation",
        observation: "Recorded snapshot · not connected live",
        connection: "recorded",
        animateWork: false,
        outputAgeSeconds: null,
        heartbeatAgeMs: null,
        receiptMarks: [],
        facts: [
          {
            label: "Viewer",
            value: "Local recording. No live subscription or session controls.",
          },
          {
            label: "Machine at capture",
            value: `${data.host}: ${actual.host.state}, observed ${clockLabel(actual.host.observed_at)}`,
          },
          {
            label: "Provider at capture",
            value: `${actual.activity.state}, observed ${clockLabel(actual.activity.observed_at)}`,
          },
          {
            label: "Evidence expires",
            value: clockLabel(actual.activity.valid_until),
          },
          {
            label: "Server transcript at capture",
            value: actual.transcript.convergence,
          },
        ],
      },
    };
  }

  if (scene === "quiet") {
    arrivals = [];
    headline = "Thinking";
    detail = "Work is confirmed; no new output yet.";
    detailKind = "explanation";
  }
  if (scene === "expiry") {
    arrivals = availableReceipts.filter((at) => at <= Math.min(time, 4400));
    providerFact = `Simulated provider evidence expires at replay 0:12${time >= 12000 ? " (expired)" : ""}`;
    if (time >= 12000) {
      tone = "unknown";
      headline = "Work status unconfirmed";
      detail = `Last reported: ${action}.`;
      detailKind = "explanation";
      animateWork = false;
    }
  }
  if (scene === "reconnect") {
    const before = availableReceipts.filter((at) => at < 5000);
    const replayTimes = [12200, 12800, 13600, 14500, 15500, 16800];
    const after = [18800, 20800, 23800, 26800];
    arrivals = [...before, ...replayTimes, ...after]
      .slice(0, availableReceipts.length)
      .filter((at) => at <= time);
    if (time >= 5000 && time < 18000) {
      animateWork = false;
      tone = "unknown";
      headline = "Work status unconfirmed";
      detailKind = "explanation";
      if (time < 12000) {
        connection = "reconnecting";
        observation = "Reconnecting";
        heartbeatAgeMs = null;
        detail = `Last reported: ${action}.`;
      } else {
        connection = "checking";
        observation = "Reconnected · updating transcript";
        detail = "Receiving earlier output, not live work.";
      }
      providerFact =
        "Last-known work only. Reconnection and old output do not renew provider evidence.";
      transcriptFact =
        time < 12000
          ? "Preserved last received transcript"
          : "Applying a fresh snapshot; replay content is not current provider work";
    }
  }
  if (scene === "machine" && time >= 8000) {
    arrivals = availableReceipts.filter((at) => at < 8000);
    tone = "unknown";
    headline = "Work status unconfirmed";
    animateWork = false;
    observation = `Connected · can’t reach ${data.host}`;
    detail = "The agent may still be running.";
    detailKind = "explanation";
    hostFact = `Simulated fresh host fact at replay 0:08: ${data.host} unreachable`;
  }
  if (scene === "return") {
    arrivals = availableReceipts
      .map((at) => at + 5000)
      .filter((at) => at <= time);
    if (time < 12000) {
      tone = "unknown";
      animateWork = false;
      headline =
        time < 5000 ? "Checking current status" : "Work status unconfirmed";
      observation =
        time < 5000
          ? "Reconnecting · showing saved transcript"
          : "Connected · transcript refreshed";
      connection = time < 5000 ? "checking" : "connected";
      heartbeatAgeMs = time < 5000 ? null : (time - 5000) % 5000;
      detail = "Checking freshness of the saved state.";
      detailKind = "explanation";
      providerFact =
        "Cached evidence is not live; new provider observation scheduled for replay 0:12";
    }
  }
  if (scene === "attention" && time >= 6000) {
    arrivals = availableReceipts.filter((at) => at < 6000);
    tone = "attention";
    headline = "Needs your approval";
    animateWork = false;
    detail = data.command;
    observation = "Connected · waiting for your response";
    providerFact =
      "Simulated explicit permission request, not inferred from needs_user or silence";
  }
  if (scene === "finished" && time >= 10000) {
    tone = "quiet";
    headline = "Turn finished";
    animateWork = false;
    detail = "Recorded output remains available.";
    detailKind = "explanation";
    arrivals = availableReceipts.filter((at) => at < 10000);
    observation = "Connected · ready for your next instruction";
    providerFact =
      "Simulated explicit terminal turn fact; no success outcome inferred";
  }
  const outputAgeSeconds =
    scene === "quiet"
      ? 40 + Math.floor(time / 1000)
      : arrivals.length
        ? Math.floor((time - arrivals[arrivals.length - 1]) / 1000)
        : null;
  const receiptMarks: ReceiptMark[] = arrivals
    .map((at, sequence) => ({
      id: `receipt-${at}`,
      ageMs: time - at,
      sequence,
      replay:
        (scene === "reconnect" && at >= 12000 && at < 18000) ||
        (scene === "return" && at < 12000),
    }))
    .filter((mark) => mark.ageMs < 12000);
  const end =
    scene === "finished" && time >= 10000
      ? data.items.length
      : Math.min(data.items.length, data.baseEnd + arrivals.length);
  const stageKey = `${end}:${animateWork}`;
  let projectedItems = data.stages.get(stageKey);
  if (!projectedItems) {
    const items = data.items.slice(0, end);
    // Archive call states can describe a later point than our replay cursor.
    // Only a visible unmatched call with valid simulated activity may appear running.
    const resultIds = new Set(
      items
        .filter((item) => item.event?.role === "tool")
        .map((item) => item.event?.tool_call_id)
        .filter(Boolean),
    );
    projectedItems = items.map((item) => {
      const event = item.event;
      if (
        !event?.tool_name ||
        event.role !== "assistant" ||
        !event.tool_call_id ||
        resultIds.has(event.tool_call_id)
      )
        return item;
      return {
        ...item,
        event: {
          ...event,
          tool_call_state: animateWork ? ("running" as const) : null,
        },
      };
    });
    data.stages.set(stageKey, projectedItems);
  }
  return {
    items: projectedItems,
    explanation: description,
    sourceLabel: "Design replay · recorded content · simulated states",
    notice:
      scene === "finished" && time >= 10000 && time < 14000
        ? "Output remains available in the transcript."
        : scene === "reconnect" && time >= 18000 && time < 22000
          ? "Fresh work evidence restored."
          : null,
    ribbon: {
      tone,
      headline,
      detail,
      detailKind,
      observation,
      connection,
      animateWork,
      outputAgeSeconds,
      heartbeatAgeMs,
      receiptMarks,
      facts: [
        {
          label: "Viewer connection",
          value:
            connection === "reconnecting"
              ? "Simulated disconnected viewer"
              : connection === "checking"
                ? "Simulated reconciliation in progress"
                : "Simulated connected viewer; heartbeats do not prove provider work",
        },
        { label: "Machine", value: hostFact },
        { label: "Provider evidence", value: providerFact },
        { label: "Transcript", value: transcriptFact },
        {
          label: "Data provenance",
          value: `Real session ${capture.source.sessionId}. Original timestamps preserved; replay cadence is synthetic.`,
        },
      ],
    },
  };
}
