/**
 * SessionChat - Live-send dock for timeline sessions.
 *
 * Features:
 * - Lock status indicators
 * - Error handling with retry
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useQuery, useQueries, useQueryClient } from "@tanstack/react-query";
import {
  cancelSessionInput,
  fetchSessionInput,
  fetchSessionInputs,
  fetchSessionLockStatus,
  interruptLiveSession,
  interruptConsoleTurn,
  postSessionInput,
  postSessionInputMultipart,
  type ConsoleTurnReceipt,
  type QueuedInputSummary,
  type SessionInputResponse,
  type SessionLockInfo,
} from "@/shared/api/index";
import type { AgentSession } from "@/shared/api/agents";
import { HOST_LINK_COPY } from "@/shared/hostLink/copy";
import { hostLinkStore } from "@/shared/hostLink/store";
import { refreshAgentSessionProjectionTail } from "@/shared/api/useAgentSessions";
import type {
  ManagedLaunchSuggestion,
  TimelineItem,
} from "@/shared/session/model";
import { useComposerAttachments } from "./useComposerAttachments";
import { Button } from "@/shared/ui";
import { AttachmentTray } from "./AttachmentTray";
import { isSessionClosed } from "@/shared/session/sessionRuntime";
import { copyToClipboard } from "@/shared/lib/clipboard";
import { ManagedLaunchHintCard } from "../ManagedLaunchHintCard";
import type { OutboxEntry } from "../OutboxRow";
import { Nixie } from "@/shared/instruments/Nixie";
import { StatusBulb } from "@/shared/instruments/StatusLamp";
import { findRunningTool, getRunningTurnStartMs } from "@/shared/instruments/toolActivity";
import {
  formatClockTime,
  formatElapsedClock,
  getSessionHeaderState,
} from "../sessionHeaderState";
import type { SessionActivityFeed } from "../sessionActivityFeed";
import { BackgroundTaskRoster } from "../BackgroundTaskRoster";
import { delegatedWorkDetailFromFacts } from "../SessionRuntimeStrip";
import ModelPicker from "@/features/launch/ModelPicker";
import { useMediaQuery } from "@/shared/hooks/useMediaQuery";
import { useWallClock } from "@/shared/hooks/useWallClock";
import { getWindowActivity, useWindowActivity } from "@/shared/hooks/useWindowActivity";
import {
  isActivityExecuting,
  isActivityStalled,
} from "@/shared/session/activityEvidence";
import {
  delegatedWorkLabel,
  workingStatusLabel,
} from "@/shared/session/sessionStatus";
import {
  failedBeforeRecorded,
  isSettledDelivery,
  matchSendsToTranscript,
} from "./unrecordedInputs";
import { parseUTC } from "@/shared/lib/dateUtils";
import "./session-chat.css";

interface PendingManagedLocalInput {
  text: string;
  clientRequestId: string;
  /** When the user sent it (ISO, client clock): the row's "You asked at". */
  sentAt: string;
  serverInputId: number | null;
  serverLiveInputId?: string | null;
  intent: "auto" | "queue" | "steer";
  model?: string | null;
  legacyModelMissing?: boolean;
  attachments: { blob: Blob; filename: string }[];
  attachmentSummaries?: { filename: string; mimeType: string | null; byteSize: number }[];
  /** The stored images were lost from this browser, so a same-ID retry would
   * resend text without them. Recovery comes only from the server receipt. */
  attachmentsLost?: boolean;
  /** Server ownership phase; Console turns remain here until terminal. */
  phase:
    | "submitting"
    | "queued"
    | "starting"
    | "active"
    | "draining"
    | "unknown"
    | "failed"
    | "delivered";
  deliveryStatus?: string | null;
  detail?: string | null;
}

type ManagedSendResult =
  | { kind: "local-persistence-failed"; error: string }
  | { kind: "accepted"; clientRequestId: string }
  | { kind: "rejected"; clientRequestId: string; error: string }
  | { kind: "unknown"; clientRequestId: string; error: string };

interface ManagedSendOptions {
  existingClientRequestId?: string;
  /** How many automatic same-ID re-sends preceded this attempt. */
  autoRetryAttempt?: number;
  replacementClientRequestId?: string;
  model?: string | null;
  legacyModelMissing?: boolean;
  onPersisted?: () => void;
}

// A send that never reached the server (network drop, a deploy restart's
// 502/503/504, a draining runtime) is re-sent under the same request ID, so
// the server's idempotency makes it land at most once. The row keeps saying
// "Sending…" while this runs; only an exhausted budget becomes "Not confirmed".
const AUTO_RETRY_DELAYS_MS = [1_000, 2_000, 3_000, 5_000, 8_000, 10_000, 10_000, 15_000, 15_000, 20_000];
const RECONNECTING_DETAIL = "reconnecting to Longhouse";

function isTransientSendFailure(error: unknown, structured: { error_code?: string } | null): boolean {
  if (structured?.error_code === "runtime_draining") return true;
  // fetch rejects with a TypeError only for a network failure; its message
  // differs by engine. Anything else is a bug and must surface, not retry.
  if (error instanceof TypeError && /fetch|network|load failed/i.test(error.message)) return true;
  const status = error && typeof error === "object" && "status" in error ? (error as { status: unknown }).status : null;
  return status === 502 || status === 503 || status === 504;
}

/** A delivered input keeps a lightweight row until its exact transcript echo. */
const IDEMPOTENCY_CONFLICT_ERROR =
  "Not sent: this request ID already belongs to a different message.";
const ATTACHMENTS_LOST_ERROR =
  "Not confirmed, and this browser lost its images; send it again with the images.";
const UNCONFIRMED_DELIVERY_ERROR =
  "Delivery is not confirmed; retry with the same request.";
// The server keeps a Console turn fresh while its machine reports the run (a
// long tool call included), so a stale turn means no signal, not a failure.
// When the receipt says the agent took the message, the row still says "Sent".
const STALE_CONSOLE_TURN_DETAIL =
  "no recent signal from its machine, so its status is unknown";
const LEGACY_MODEL_WARNING =
  "Original model was not recorded; retry uses the server default and may not reproduce the original run.";

type ActiveConsoleTurnPhase =
  | "queued"
  | "starting"
  | "active"
  | "draining";

function activeConsoleTurnPhase(
  state: string | null | undefined,
): ActiveConsoleTurnPhase | null {
  return state === "queued" ||
    state === "starting" ||
    state === "active" ||
    state === "draining"
    ? state
    : null;
}
interface SessionChatProps {
  session: SessionChatTarget;
  composerPlaceholder?: string;
  submitLabel?: string;
  requireClickForFirstSend?: boolean;
  keyboardHintText?: string;
  /** Managed-local sessions use explicit live-send with fast JSON ack. */
  chatMode?: "managed_local";
  composerDisabledReason?: string | null;
  /// Heading over the disabled-composer copy. Derived from the typed blocker;
  /// falls back only when a caller has nothing better.
  composerDisabledTitle?: string | null;
  composerDisabledAction?: ReactNode;
  managedLaunchSuggestion?: ManagedLaunchSuggestion | null;
  /**
   * When true, sending while the session is locked persists as a queued
   * input that auto-dispatches at the next turn boundary. Gated by the
   * `can_queue_next_input` capability on managed-local sessions.
   */
  canQueueNextInput?: boolean;
  /**
   * When true, the managed transport supports mid-turn steer. Shows a
   * primary "Send" action while the session is working (Enter); queueing
   * becomes a secondary action (Tab). Turn-ended races surface as an inline
   * error with a "Queue instead" affordance.
   */
  canSteerActiveTurn?: boolean;
  /**
   * Durable timeline rows visible in the parent workspace. When present,
   * managed-local optimistic inputs stay visible until the backend-authored
   * user row with matching input identity arrives.
   */
  timelineItems?: TimelineItem[];
  /** Live per-frame stream feed; drives the composer top-edge pulse on real packets. */
  activityFeed?: SessionActivityFeed | null;
  /**
   * The runtime strip's evidence-disclosure icon. It
   * rides along the input's bottom edge, and in the head row while the
   * composer is unavailable.
   */
  composerHeaderAccessory?: ReactNode;
  /**
   * When provided, in-flight, queued, and failed sends are reported here for
   * the transcript to render at its tail instead of stacking inside the
   * composer.
   */
  onOutboxChange?: (entries: OutboxEntry[]) => void;
  /** The session's live workspace stream is connected and fresh. */
  streamConnected?: boolean;
  editedFilesLabel?: string | null;
  editedFilesTitle?: string | null;
}

export type SessionChatTarget = Pick<
  AgentSession,
  | "id"
  | "project"
  | "provider"
  | "device_id"
  | "selected_model"
  | "capabilities"
  | "session_state"
  | "origin_kind"
  | "pending_background_agent_count"
>;
function newClientRequestId(): string {
  const randomUUID = globalThis.crypto?.randomUUID?.bind(globalThis.crypto);
  if (randomUUID) return `web-${randomUUID()}`;
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

const INPUT_OUTBOX_PREFIX = "longhouse:session-input:";

interface StoredInputOutbox {
  sessionId: string;
  text: string;
  intent: "auto" | "queue" | "steer";
  clientRequestId: string;
  /** Null is an explicit no-override selection; absent means legacy metadata. */
  model?: string | null;
  attachments: { filename: string; type: string; size: number }[];
  createdAt: number;
  deliveryConfirmed?: boolean;
  waitingForHostUpdate?: boolean;
}

interface StoredInputOutboxPayload {
  attachments: { filename: string; blob: Blob }[];
}

const INPUT_OUTBOX_DB = "longhouse-input-outbox";
const INPUT_OUTBOX_STORE = "payloads";
let inputOutboxDbPromise: Promise<IDBDatabase> | null = null;

function inputOutboxKey(sessionId: string, clientRequestId: string): string {
  return `${INPUT_OUTBOX_PREFIX}${sessionId}:${clientRequestId}`;
}

function openInputOutboxDb(): Promise<IDBDatabase> {
  if (inputOutboxDbPromise) return inputOutboxDbPromise;
  if (typeof indexedDB === "undefined") {
    return Promise.reject(
      new Error("IndexedDB is unavailable; attachment cannot be persisted"),
    );
  }
  const pending = new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(INPUT_OUTBOX_DB, 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore(INPUT_OUTBOX_STORE);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(request.error ?? new Error("Could not open attachment storage"));
  });
  inputOutboxDbPromise = pending;
  return pending;
}

async function writeInputOutboxPayload(
  sessionId: string,
  clientRequestId: string,
  payload: StoredInputOutboxPayload,
): Promise<void> {
  const db = await openInputOutboxDb();
  await new Promise<void>((resolve, reject) => {
    const request = db
      .transaction(INPUT_OUTBOX_STORE, "readwrite")
      .objectStore(INPUT_OUTBOX_STORE)
      .put(payload, inputOutboxKey(sessionId, clientRequestId));
    request.onsuccess = () => resolve();
    request.onerror = () =>
      reject(
        request.error ?? new Error("Could not persist attachment payload"),
      );
  });
}

async function readInputOutboxPayload(
  sessionId: string,
  clientRequestId: string,
): Promise<StoredInputOutboxPayload | null> {
  const db = await openInputOutboxDb();
  return new Promise<StoredInputOutboxPayload | null>((resolve, reject) => {
    const request = db
      .transaction(INPUT_OUTBOX_STORE, "readonly")
      .objectStore(INPUT_OUTBOX_STORE)
      .get(inputOutboxKey(sessionId, clientRequestId));
    request.onsuccess = () =>
      resolve((request.result as StoredInputOutboxPayload | undefined) ?? null);
    request.onerror = () =>
      reject(request.error ?? new Error("Could not read attachment payload"));
  });
}

function deleteInputOutboxPayload(
  sessionId: string,
  clientRequestId: string,
): void {
  void openInputOutboxDb()
    .then((db) => {
      return new Promise<void>((resolve) => {
        const request = db
          .transaction(INPUT_OUTBOX_STORE, "readwrite")
          .objectStore(INPUT_OUTBOX_STORE)
          .delete(inputOutboxKey(sessionId, clientRequestId));
        request.onsuccess = () => resolve();
        request.onerror = () => resolve();
      });
    })
    .catch(() => undefined);
}
async function persistInputOutbox(
  sessionId: string,
  pending: {
    text: string;
    intent: "auto" | "queue" | "steer";
    clientRequestId: string;
    model?: string | null;
    attachments: { blob: Blob; filename: string }[];
  },
): Promise<void> {
  if (typeof window === "undefined") {
    throw new Error("Input storage is unavailable outside a browser");
  }
  const stored: StoredInputOutbox = {
    sessionId,
    text: pending.text,
    intent: pending.intent,
    clientRequestId: pending.clientRequestId,
    model: pending.model,
    attachments: pending.attachments.map(({ blob, filename }) => ({
      filename,
      type: blob.type || "application/octet-stream",
      size: blob.size,
    })),
    createdAt: Date.now(),
    deliveryConfirmed: false,
  };
  if (pending.attachments.length > 0) {
    await writeInputOutboxPayload(sessionId, pending.clientRequestId, {
      attachments: pending.attachments.map(({ blob, filename }) => ({
        blob,
        filename,
      })),
    });
  }
  try {
    window.localStorage.setItem(
      inputOutboxKey(sessionId, pending.clientRequestId),
      JSON.stringify(stored),
    );
  } catch (storageError) {
    deleteInputOutboxPayload(sessionId, pending.clientRequestId);
    throw new Error("Could not persist input intent before sending", {
      cause: storageError,
    });
  }
}

function setInputWaitingForHostUpdate(
  sessionId: string,
  clientRequestId: string,
  waiting: boolean,
): void {
  const key = inputOutboxKey(sessionId, clientRequestId);
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return;
    const stored = JSON.parse(raw) as StoredInputOutbox;
    if (
      stored.sessionId !== sessionId ||
      stored.clientRequestId !== clientRequestId
    ) {
      return;
    }
    if (waiting) stored.waitingForHostUpdate = true;
    else delete stored.waitingForHostUpdate;
    window.localStorage.setItem(key, JSON.stringify(stored));
  } catch {
    // The already-persisted send remains safe to retry in the mounted view.
  }
}

function clearInputOutbox(sessionId: string, clientRequestId: string): void {
  try {
    window.localStorage.removeItem(inputOutboxKey(sessionId, clientRequestId));
  } finally {
    deleteInputOutboxPayload(sessionId, clientRequestId);
  }
}

function retainDeliveredInputOutbox(
  sessionId: string,
  clientRequestId: string,
): void {
  if (typeof window === "undefined") return;
  const key = inputOutboxKey(sessionId, clientRequestId);
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return;
    const stored = JSON.parse(raw) as StoredInputOutbox;
    if (
      stored.sessionId !== sessionId ||
      stored.clientRequestId !== clientRequestId
    ) {
      return;
    }
    const delivered = { ...stored, deliveryConfirmed: true };
    delete delivered.waitingForHostUpdate;
    window.localStorage.setItem(key, JSON.stringify(delivered));
    deleteInputOutboxPayload(sessionId, clientRequestId);
  } catch {
    // Keep the original durable payload when the summary cannot be stored.
  }
}

function summarizeInputAttachments(
  attachments: { blob: Blob; filename: string }[],
): { filename: string; mimeType: string | null; byteSize: number }[] {
  return attachments.map((attachment) => ({
    filename: attachment.filename,
    mimeType: attachment.blob.type || null,
    byteSize: attachment.blob.size,
  }));
}

const INPUT_DISMISSALS_PREFIX = "longhouse:session-input-dismissals:";

function inputDismissalsKey(sessionId: string): string {
  return `${INPUT_DISMISSALS_PREFIX}${sessionId}`;
}

function readDismissedInputIds(sessionId: string): Set<string> {
  try {
    const value = window.localStorage.getItem(inputDismissalsKey(sessionId));
    const parsed: unknown = value ? JSON.parse(value) : [];
    return new Set(
      Array.isArray(parsed)
        ? parsed.filter((id): id is string => typeof id === "string" && id.length > 0)
        : [],
    );
  } catch {
    return new Set();
  }
}

function dismissInputReceipt(sessionId: string, clientRequestId: string): void {
  const dismissed = readDismissedInputIds(sessionId);
  if (dismissed.has(clientRequestId)) return;
  dismissed.add(clientRequestId);
  window.localStorage.setItem(
    inputDismissalsKey(sessionId),
    JSON.stringify(Array.from(dismissed)),
  );
}

function readInputOutboxes(sessionId: string): StoredInputOutbox[] {
  const prefix = `${INPUT_OUTBOX_PREFIX}${sessionId}:`;
  const entries: StoredInputOutbox[] = [];
  for (let index = 0; index < window.localStorage.length; index += 1) {
    const key = window.localStorage.key(index);
    if (!key || !key.startsWith(prefix)) continue;
    try {
      const raw = window.localStorage.getItem(key);
      if (!raw) continue;
      const parsed = JSON.parse(raw) as StoredInputOutbox;
      if (
        parsed.sessionId === sessionId &&
        parsed.clientRequestId &&
        key === inputOutboxKey(sessionId, parsed.clientRequestId)
      ) {
        entries.push(parsed);
      }
    } catch {
      // Ignore one malformed slot without hiding other operation identities.
    }
  }
  return entries.sort((lhs, rhs) => {
    if (lhs.createdAt === rhs.createdAt) {
      return lhs.clientRequestId.localeCompare(rhs.clientRequestId);
    }
    return lhs.createdAt - rhs.createdAt;
  });
}

async function loadInputOutboxes(
  sessionId: string,
): Promise<
  {
    metadata: StoredInputOutbox;
    attachments: { blob: Blob; filename: string }[];
    attachmentsLost: boolean;
  }[]
> {
  const metadata = readInputOutboxes(sessionId);
  const loaded: {
    metadata: StoredInputOutbox;
    attachments: { blob: Blob; filename: string }[];
    attachmentsLost: boolean;
  }[] = [];
  for (const entry of metadata) {
    if (entry.deliveryConfirmed || entry.attachments.length === 0) {
      loaded.push({ metadata: entry, attachments: [], attachmentsLost: false });
      continue;
    }
    const payload = await readInputOutboxPayload(
      sessionId,
      entry.clientRequestId,
    );
    // One lost IndexedDB record must not drop every other stored send. Keep
    // the operation so its row can still settle from the server's receipt,
    // but never offer a retry that would silently drop its images.
    loaded.push({
      metadata: entry,
      attachments: payload?.attachments ?? [],
      attachmentsLost: !payload,
    });
  }
  return loaded;
}
function durableSubmittedInputRowId(
  timelineItems: TimelineItem[],
  pendingInput: PendingManagedLocalInput,
): string | null {
  const match = timelineItems.find((item) => {
    if (item.kind !== "message") return false;
    const { event } = item;
    if (event.role !== "user" || event.is_head_branch === false) return false;
    const origin = event.input_origin;
    if (!origin || origin.authored_via !== "longhouse") return false;
    if (
      pendingInput.serverInputId != null &&
      origin.session_input_id === pendingInput.serverInputId
    ) {
      return true;
    }
    return Boolean(
      origin.client_request_id &&
      origin.client_request_id === pendingInput.clientRequestId,
    );
  });
  return match?.kind === "message" ? String(match.event.id) : null;
}

function inputErrorCode(error?: string | null): string | null {
  const normalized = error?.trim().toLowerCase();
  if (!normalized) return null;
  return normalized.split(":", 1)[0].trim();
}

function hasRuntimeDrainingError(error?: string | null): boolean {
  return inputErrorCode(error) === "runtime_draining";
}

function hasProviderDeliveryUnknownError(error?: string | null): boolean {
  const code = inputErrorCode(error);
  return (
    code === "delivery_unknown" ||
    code === "provider_unknown" ||
    code === "provider_delivery_unknown" ||
    code === "input_receipt_unknown"
  );
}

// The server has the input: a Console turn is running on it, or delivery
// finished. The user's row reads "Sent" and clears once its echo appears;
// what the agent is doing belongs to the activity headline, not the row.
const ACCEPTED_INPUT_PHASES = new Set(["starting", "active", "draining", "delivered"]);

// A delivered input whose Console turn was then stopped: the message reached
// the provider and is in the transcript. Only an input cancelled before
// delivery (receipt status "cancelled") was never sent.
function stoppedAfterDelivery(row: {
  status?: string | null;
  turn?: { state?: string | null } | null;
} | null | undefined): boolean {
  return row?.status === "delivered" && row?.turn?.state === "cancelled";
}

// A receipt linked to its durable transcript event is in the transcript, so
// it was delivered whatever its Console turn says: a turn left nonterminal
// because its run-end never arrived says nothing about the message. The
// transcript row already shows it, so it is never an outbox row and never
// "Not confirmed". One rule for every path that reads a receipt.
function inTranscript(row: { durable_event_id?: string | null } | null | undefined): boolean {
  return Boolean(row?.durable_event_id);
}

// A receipt linked to its transcript event says the transcript has the
// message, but this browser may not have loaded that row yet: the server
// links the receipt and ingests the row in a race, and the read that brings
// the row in takes time. Until the loaded rows show it, the receipt keeps the
// user's row on screen, for at most this long after this browser first saw
// it linked (this browser's clock, so a skewed clock cannot shorten it).
const LINKED_RECEIPT_BRIDGE_MS = 60_000;
// Only a recent send is bridged: an old linked receipt loaded with the page
// is history the transcript already shows. Loose on purpose, since it
// compares the server's clock with this browser's.
/** A connected page's one read after a send, in case the stream dropped its wake. */
const SEND_SAFETY_READ_MS = 5_000;
const LINKED_RECEIPT_RECENT_MS = 10 * 60_000;

function bridgeReceiptKey(row: QueuedInputSummary): string | null {
  return row.client_request_id ?? row.live_input_id ?? (row.id != null ? String(row.id) : null);
}

function linkedReceiptBridgeEndsAt(
  row: QueuedInputSummary,
  firstSeenLinkedAt: Map<string, number>,
  now: number,
): number | null {
  if (!inTranscript(row) || !row.created_at) return null;
  if (row.origin === "wake" || row.origin === "longhouse" || row.turn?.origin === "wake") {
    return null;
  }
  const created = parseUTC(row.created_at).getTime();
  if (!Number.isFinite(created) || now - created > LINKED_RECEIPT_RECENT_MS) return null;
  const key = bridgeReceiptKey(row);
  if (!key) return null;
  let seen = firstSeenLinkedAt.get(key);
  if (seen === undefined) {
    seen = now;
    firstSeenLinkedAt.set(key, seen);
  }
  return seen + LINKED_RECEIPT_BRIDGE_MS;
}

const NONTERMINAL_CONSOLE_TURN_STATES = new Set([
  "queued",
  "starting",
  "active",
  "draining",
]);

function hasUnknownDeliveryError(error?: string | null): boolean {
  return (
    hasProviderDeliveryUnknownError(error) || hasRuntimeDrainingError(error)
  );
}

interface StructuredInputErrorDetail {
  error_code?: string;
  disposition?: "accepted" | "rejected" | "unknown";
  delivery_status?: string | null;
  client_request_id?: string | null;
  input_id?: number | null;
  live_input_id?: string | null;
  turn?: ConsoleTurnReceipt | null;
  message?: string;
}

interface TurnEndedDraft {
  clientRequestId: string;
  text: string;
  model: string | null;
}

function structuredInputErrorDetail(error: unknown): StructuredInputErrorDetail | null {
  if (!error || typeof error !== "object" || !("body" in error)) return null;
  const body = error.body;
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const detail = body.detail;
  return detail && typeof detail === "object"
    ? (detail as StructuredInputErrorDetail)
    : null;
}

export function SessionChat({
  session,
  composerPlaceholder,
  submitLabel = "Send",
  requireClickForFirstSend = false,
  keyboardHintText,
  chatMode,
  composerDisabledReason = null,
  composerDisabledTitle = null,
  composerDisabledAction,
  managedLaunchSuggestion = null,
  canQueueNextInput = false,
  canSteerActiveTurn = false,
  timelineItems,
  activityFeed = null,
  composerHeaderAccessory,
  onOutboxChange,
  streamConnected = false,
  editedFilesLabel = null,
  editedFilesTitle = null,
}: SessionChatProps) {
  const outboxInTranscript = Boolean(onOutboxChange);
  const windowActivity = useWindowActivity();
  const activity = session.session_state.activity;
  const renderNowMs = Date.now();
  const activityNowMs = Math.max(
    renderNowMs,
    useWallClock(
      renderNowMs <= Date.parse(activity.valid_until ?? ""),
      windowActivity === "ambient" ? 15_000 : 1_000,
    ),
  );
  const isSessionExecuting = isActivityExecuting(activity, activityNowMs);
  const isStalled = isActivityStalled(activity, activityNowMs);
  const isManagedLocal = chatMode === "managed_local";
  const isComposerDisabled = Boolean(composerDisabledReason);
  const attachImagesEnabled =
    isManagedLocal && Boolean(session.capabilities?.attach_images);
  const composerAttachments = useComposerAttachments();
  const hasHadComposer = useRef(false);
  if (!isComposerDisabled) hasHadComposer.current = true;
  const retainDockComposer =
    hasHadComposer.current && !composerDisabledAction;
  const showComposerUnavailableState =
    isComposerDisabled && !retainDockComposer;
  const queryClient = useQueryClient();
  const isMobileComposer = useMediaQuery("(max-width: 767px)");
  const [draft, setDraft] = useState("");
  const [selectedModel, setSelectedModel] = useState(
    () => session.selected_model?.trim() ?? "",
  );
  const selectedModelChoiceRef = useRef({
    sessionId: session.id,
    explicit: false,
  });
  const handleSelectedModelChange = useCallback(
    (model: string) => {
      selectedModelChoiceRef.current = { sessionId: session.id, explicit: true };
      setSelectedModel(model);
    },
    [session.id],
  );
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isInterrupting, setIsInterrupting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blockedKeyboardSubmit, setBlockedKeyboardSubmit] = useState(false);
  useEffect(() => {
    if (selectedModelChoiceRef.current.sessionId !== session.id) {
      selectedModelChoiceRef.current = { sessionId: session.id, explicit: false };
    }
    if (!selectedModelChoiceRef.current.explicit) {
      setSelectedModel(session.selected_model?.trim() ?? "");
    }
  }, [session.id, session.selected_model]);

  const [sentConfirmation, setSentConfirmation] = useState(false);
  const [pendingManagedLocalInputs, setPendingManagedLocalInputs] = useState<
    PendingManagedLocalInput[]
  >([]);
  const ownsManagedInputs =
    isManagedLocal ||
    session.session_state?.control?.ownership === "owned" ||
    pendingManagedLocalInputs.length > 0;
  const pendingOutboxSessionRef = useRef<string | null>(null);
  useEffect(() => {
    let mounted = true;
    const sessionChanged = pendingOutboxSessionRef.current !== session.id;

    // Clear the previous session before reading the new scoped outbox. The
    // load is intentionally tied to the transition itself so an empty pending
    // list cannot suppress first-mount/reload hydration. React may invoke an
    // effect setup twice in development; the second setup must still hydrate.
    if (sessionChanged) {
      pendingOutboxSessionRef.current = session.id;
      setPendingManagedLocalInputs([]);
    }
    void loadInputOutboxes(session.id)
      .then((stored) => {
        if (!mounted || stored.length === 0) return;
        const recovered: PendingManagedLocalInput[] = stored.map(
          ({ metadata, attachments, attachmentsLost }) => {
            const waitingForHostUpdate =
              !metadata.deliveryConfirmed &&
              metadata.waitingForHostUpdate === true &&
              !attachmentsLost;
            return {
              text: metadata.text,
              clientRequestId: metadata.clientRequestId,
              sentAt: new Date(
                Number.isFinite(metadata.createdAt) ? metadata.createdAt : Date.now(),
              ).toISOString(),
              serverInputId: null,
              serverLiveInputId: null,
              intent: metadata.intent,
              model: metadata.model,
              legacyModelMissing: !Object.prototype.hasOwnProperty.call(metadata, "model"),
              attachments,
              attachmentSummaries: metadata.attachments.map((attachment) => ({
                filename: attachment.filename,
                mimeType: attachment.type || null,
                byteSize: attachment.size,
              })),
              phase: metadata.deliveryConfirmed
                ? "delivered"
                : waitingForHostUpdate
                  ? "submitting"
                  : "unknown",
              attachmentsLost,
              detail: attachmentsLost
                ? ATTACHMENTS_LOST_ERROR
                : waitingForHostUpdate
                  ? HOST_LINK_COPY.sendQueued
                  : undefined,
            };
          },
        );
        setPendingManagedLocalInputs(recovered);
      })
      .catch((storageError) => {
        if (mounted) {
          setError(
            storageError instanceof Error
              ? storageError.message
              : "Could not recover the stored input",
          );
        }
      });
    return () => {
      mounted = false;
    };
  }, [session.id]);

  const composerTextareaRef = useRef<HTMLTextAreaElement>(null);
  // Automatic re-sends belong to this mounted view; a remount rehydrates the
  // stored operation instead.
  const autoRetryTimersRef = useRef(new Set<ReturnType<typeof setTimeout>>());
  useEffect(() => {
    const timers = autoRetryTimersRef.current;
    return () => {
      for (const timer of timers) clearTimeout(timer);
      timers.clear();
    };
  }, []);
  const hostResumeRetryWaitersRef = useRef(
    new Map<string, (() => void) | null>(),
  );
  useEffect(() => {
    const waiters = hostResumeRetryWaitersRef.current;
    return () => {
      for (const cancel of waiters.values()) cancel?.();
      waiters.clear();
    };
  }, [session.id]);
  const sentConfirmationTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );

  const autoResizeDockTextarea = useCallback((el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    if (el.scrollHeight > 0) {
      el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
    }
  }, []);

  useEffect(() => {
    return () => {
      if (sentConfirmationTimerRef.current)
        clearTimeout(sentConfirmationTimerRef.current);
    };
  }, []);

  const handleDraftChange = useCallback((nextDraft: string) => {
    setDraft(nextDraft);
    if (!nextDraft.trim()) {
      setBlockedKeyboardSubmit(false);
      if (composerTextareaRef.current) {
        composerTextareaRef.current.style.height = "auto";
      }
    }
  }, []);

  const refreshCurrentSessionWorkspace = useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: ["agent-session-thread", session.id],
      }),
      refreshAgentSessionProjectionTail(queryClient, session.id),
      queryClient.invalidateQueries({
        queryKey: ["agent-session-events", session.id],
      }),
      queryClient.invalidateQueries({
        queryKey: ["agent-session-events-infinite", session.id],
      }),
      queryClient.invalidateQueries({ queryKey: ["session-inputs", session.id] }),
      queryClient.invalidateQueries({ queryKey: ["session-input", session.id] }),
      queryClient.invalidateQueries({ queryKey: ["agent-sessions"] }),
    ]);
  }, [queryClient, session.id]);
  // After a send the server wakes the live stream (its runtime state, then the
  // ingested rows), so a connected page needs no read of its own; the POST
  // answer already put the receipt in the cache. Without a live stream, read.
  // The fanout drops a wake when a subscriber's queue is full, so a connected
  // page still reads the transcript's newest rows once, a few seconds on
  // (a delta read: usually one small request, off the path that paints).
  const streamConnectedRef = useRef(streamConnected);
  streamConnectedRef.current = streamConnected;
  const safetyReadRef = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (safetyReadRef.current !== null) window.clearTimeout(safetyReadRef.current);
    },
    [],
  );
  const refreshAfterSend = useCallback(() => {
    if (!streamConnectedRef.current) {
      void refreshCurrentSessionWorkspace();
      return;
    }
    if (safetyReadRef.current !== null) window.clearTimeout(safetyReadRef.current);
    safetyReadRef.current = window.setTimeout(() => {
      safetyReadRef.current = null;
      void refreshAgentSessionProjectionTail(queryClient, session.id);
    }, SEND_SAFETY_READ_MS);
  }, [queryClient, refreshCurrentSessionWorkspace, session.id]);
  // A send whose POST is still out: its own answer will say what happened,
  // so its receipt is not polled meanwhile.
  const postsInFlightRef = useRef(new Set<string>());
  // Receipts the loaded rows show although the server never linked them,
  // matched by the same rules as the transcript pane (matchSendsToTranscript):
  // each receipt remembers which user rows were loaded when it first
  // appeared, so an older row with the same words is never taken for it.
  const receiptFirstSightRef = useRef(new Map<string, ReadonlySet<string>>());
  const receiptsShownInTranscript = useCallback(
    (rows: readonly QueuedInputSummary[]): Set<QueuedInputSummary> => {
      const shown = new Set<QueuedInputSummary>();
      const keyed = rows.filter((row) => row.client_request_id);
      const firstSight = receiptFirstSightRef.current;
      // Not before the transcript has loaded (see TimelinePane).
      let loadedUserRows: Set<string> | null = null;
      for (const row of keyed) {
        const key = row.client_request_id!;
        if (firstSight.has(key) || !timelineItems?.length) continue;
        loadedUserRows ??= new Set(
          (timelineItems ?? []).flatMap((item) =>
            item.kind === "message" && item.event.role === "user" ? [String(item.event.id)] : [],
          ),
        );
        firstSight.set(key, loadedUserRows);
      }
      if (!timelineItems?.length || keyed.length === 0) return shown;
      const matches = matchSendsToTranscript(
        keyed.map((row) => ({
          sendKey: row.client_request_id!,
          clientRequestId: row.client_request_id!,
          inputId: row.id,
          text: row.text,
          at: row.created_at,
        })),
        timelineItems,
        { loadedAtFirstSight: firstSight, boundTo: new Map() },
      );
      for (const row of keyed) if (matches.has(row.client_request_id!)) shown.add(row);
      return shown;
    },
    [timelineItems],
  );

  useEffect(() => {
    if (pendingManagedLocalInputs.length === 0 || !timelineItems) return;
    const resolvedIds = pendingManagedLocalInputs
      .filter((pending) => ACCEPTED_INPUT_PHASES.has(pending.phase))
      .filter((pending) => durableSubmittedInputRowId(timelineItems, pending) != null)
      .map((pending) => pending.clientRequestId);
    if (resolvedIds.length === 0) return;
    for (const clientRequestId of resolvedIds) {
      clearInputOutbox(session.id, clientRequestId);
    }
    setPendingManagedLocalInputs((current) =>
      current.filter((pending) => !resolvedIds.includes(pending.clientRequestId)),
    );
  }, [pendingManagedLocalInputs, session.id, timelineItems]);

  // Server confirmation releases attachment bytes; the identity-bound summary
  // remains durable until the transcript exposes the same operation.
  const markInputDelivered = useCallback(
    (
      clientRequestId: string,
      serverInputId: number | null | undefined,
      serverLiveInputId?: string | null,
    ) => {
      retainDeliveredInputOutbox(session.id, clientRequestId);
      setPendingManagedLocalInputs((current) =>
        current.map((pending) =>
          pending.clientRequestId === clientRequestId &&
          pending.phase !== "delivered"
            ? {
                ...pending,
                phase: "delivered",
                attachments: [],
                attachmentSummaries:
                  pending.attachmentSummaries ??
                  summarizeInputAttachments(pending.attachments),
                serverInputId: serverInputId ?? pending.serverInputId,
                serverLiveInputId:
                  serverLiveInputId ?? pending.serverLiveInputId,
              }
            : pending,
        ),
      );
    },
    [session.id],
  );


  const lockStatusQuery = useQuery<SessionLockInfo | null>({
    queryKey: ["session-lock", session.id],
    queryFn: async () => {
      try {
        return await fetchSessionLockStatus(session.id);
      } catch {
        return null;
      }
    },
    enabled: Boolean(session.id),
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => {
      if (windowActivity === "hidden" || !query.state.data?.locked) return false;
      return windowActivity === "ambient" ? 10_000 : 2_000;
    },
    staleTime: 15_000,
  });

  const lockInfo = useMemo(
    () =>
      lockStatusQuery.data
        ? {
            locked: lockStatusQuery.data.locked,
          }
        : null,
    [lockStatusQuery.data],
  );
  const isSendLocked = Boolean(lockInfo?.locked) || isSessionExecuting;

  const queuedInputsQuery = useQuery<QueuedInputSummary[]>({
    queryKey: ["session-inputs", session.id],
    queryFn: async () => {
      try {
        return await fetchSessionInputs(session.id);
      } catch {
        return [];
      }
    },
    enabled: Boolean(session.id) && ownsManagedInputs,
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => {
      if (windowActivity === "hidden") return false;
      const rows = query.state.data ?? [];
      const active = rows.some((row) => {
        if (inTranscript(row)) return false;
        const turnState = row.turn?.state;
        if (turnState && NONTERMINAL_CONSOLE_TURN_STATES.has(turnState)) {
          return row.turn?.is_fresh === true;
        }
        return row.status === "queued" || row.status === "delivering";
      });
      if (!active) return false;
      return windowActivity === "ambient" ? 10_000 : 2_000;
    },
    staleTime: 10_000,
  });

  const hasDeliveredLocalInput = pendingManagedLocalInputs.some((pending) =>
    ACCEPTED_INPUT_PHASES.has(pending.phase),
  );
  useEffect(() => {
    if (!hasDeliveredLocalInput || !timelineItems?.length) return;
    const refresh = setTimeout(() => {
      void queryClient.invalidateQueries({
        queryKey: ["session-inputs", session.id],
      });
      void queryClient.invalidateQueries({
        queryKey: ["session-input", session.id],
      });
    }, 250);
    return () => clearTimeout(refresh);
  }, [hasDeliveredLocalInput, queryClient, session.id, timelineItems]);
  const exactInputQueries = useQueries({
    queries: pendingManagedLocalInputs.map((pending) => ({
      queryKey: ["session-input", session.id, pending.clientRequestId],
      queryFn: () => fetchSessionInput(session.id, pending.clientRequestId),
      enabled:
        Boolean(session.id) &&
        ownsManagedInputs &&
        pending.phase !== "failed" &&
        !postsInFlightRef.current.has(pending.clientRequestId) &&
        // Delivered, and the POST answer already listed its receipt.
        !(
          pending.phase === "delivered" &&
          (queuedInputsQuery.data ?? []).some(
            (row) => row.client_request_id === pending.clientRequestId,
          )
        ),
      refetchOnWindowFocus: false,
      staleTime: 1_000,
      refetchInterval: (query: { state: { data: unknown } }) => {
        if (windowActivity === "hidden" || pending.phase === "delivered") return false;
        const pollMs = windowActivity === "ambient" ? 10_000 : 2_000;
        const row = query.state.data as QueuedInputSummary | null | undefined;
        const turnState = row?.turn?.state;
        if (turnState && NONTERMINAL_CONSOLE_TURN_STATES.has(turnState)) {
          return row?.turn?.is_fresh === true ? pollMs : false;
        }
        return pollMs;
      },
    })),
  });
  // useQueries returns fresh result arrays on render. Cache this lookup by
  // receipt facts so the parent outbox callback doesn't turn a state update
  // into an effect loop.
  const exactInputRowsKey = JSON.stringify(
    exactInputQueries.map((query, index) => {
      const row = query.data as QueuedInputSummary | null | undefined;
      return [
        pendingManagedLocalInputs[index]?.clientRequestId ?? null,
        row?.client_request_id ?? null,
        row?.id ?? null,
        row?.live_input_id ?? null,
        row?.durable_event_id ?? null,
        row?.status ?? null,
        row?.disposition ?? null,
        row?.delivery_status ?? null,
        row?.last_error ?? null,
        row?.turn?.turn_id ?? null,
        row?.turn?.run_id ?? null,
        row?.turn?.state ?? null,
        row?.turn?.is_fresh ?? null,
      ];
    }),
  );
  // A send's own receipt (polled by request id), kept only where it is the
  // freshest copy: when the list was read more recently and holds the
  // receipt, the listed copy wins everywhere it is read.
  const listedReadAt = queuedInputsQuery.dataUpdatedAt;
  const exactReadAt = new Map<string, number>();
  exactInputQueries.forEach((query, index) => {
    const clientRequestId = pendingManagedLocalInputs[index]?.clientRequestId;
    if (clientRequestId && query.data) exactReadAt.set(clientRequestId, query.dataUpdatedAt);
  });
  const exactReadAtKey = JSON.stringify([...exactReadAt]);
  const exactInputRows = useMemo(() => {
    const rows = new Map<string, QueuedInputSummary>();
    const readAt = new Map<string, number>(JSON.parse(exactReadAtKey) as [string, number][]);
    const listedIds = new Set((queuedInputsQuery.data ?? []).map((row) => row.client_request_id));
    exactInputQueries.forEach((query, index) => {
      const row = query.data;
      const clientRequestId = pendingManagedLocalInputs[index]?.clientRequestId;
      if (!row || !clientRequestId) return;
      if (listedIds.has(clientRequestId) && (readAt.get(clientRequestId) ?? 0) < listedReadAt) return;
      rows.set(clientRequestId, row);
    });
    return rows;
    // exactInputRowsKey stands for the exact queries' rows.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exactInputRowsKey, exactReadAtKey, listedReadAt, pendingManagedLocalInputs, queuedInputsQuery.data]);

  useEffect(() => {
    const listed = queuedInputsQuery.data ?? [];
    const shownByTranscript = new Set(
      Array.from(receiptsShownInTranscript(listed)).map(
        (row) => row.client_request_id,
      ),
    );
    const linkedIds = pendingManagedLocalInputs
      .filter((pending) => ACCEPTED_INPUT_PHASES.has(pending.phase))
      .filter((pending) => {
        const listedReceipt = listed.find(
          (row) => row.client_request_id === pending.clientRequestId,
        );
        const receipt =
          exactInputRows.get(pending.clientRequestId) ?? listedReceipt;
        // The transcript shows it, or its delivery settled and the served
        // receipt list now carries it: either way the server's record owns
        // the row from here, for this browser and every other client alike.
        return (
          inTranscript(receipt) ||
          shownByTranscript.has(pending.clientRequestId) ||
          (pending.phase === "delivered" &&
            listedReceipt?.created_at != null &&
            isSettledDelivery(listedReceipt) &&
            !failedBeforeRecorded(listedReceipt))
        );
      })
      .map((pending) => pending.clientRequestId);
    if (linkedIds.length === 0) return;
    // The receipt row takes over from the pending row in this same render.
    // Give the receipt list the freshest copy (the exact poll often answers
    // first) so the row does not fall back to an older state for a frame.
    const fresher = linkedIds
      .map((clientRequestId) => exactInputRows.get(clientRequestId))
      .filter((row): row is QueuedInputSummary => Boolean(row));
    if (fresher.length > 0) {
      queryClient.setQueryData<QueuedInputSummary[]>(
        ["session-inputs", session.id],
        (current) => {
          const rows = current ?? [];
          const byId = new Map(fresher.map((row) => [row.client_request_id, row]));
          const merged = rows.map((row) => byId.get(row.client_request_id) ?? row);
          for (const row of fresher) {
            if (!rows.some((existing) => existing.client_request_id === row.client_request_id)) {
              merged.push(row);
            }
          }
          return merged;
        },
      );
    }
    for (const clientRequestId of linkedIds) {
      clearInputOutbox(session.id, clientRequestId);
    }
    setPendingManagedLocalInputs((current) =>
      current.filter((pending) => !linkedIds.includes(pending.clientRequestId)),
    );
  }, [
    exactInputRows,
    pendingManagedLocalInputs,
    queryClient,
    queuedInputsQuery.data,
    receiptsShownInTranscript,
    session.id,
    timelineItems,
  ]);
  useEffect(() => {
    if (pendingManagedLocalInputs.length === 0) return;
    const nextInputs = pendingManagedLocalInputs.map<PendingManagedLocalInput>(
      (pending) => {
        const receipt =
          exactInputRows.get(pending.clientRequestId) ??
          queuedInputsQuery.data?.find(
            (row) => row.client_request_id === pending.clientRequestId,
          );
        if (!receipt) return pending;

        const turnState = receipt.turn?.state;
        const completed =
          turnState === "completed" ||
          (!turnState && receipt.status === "delivered") ||
          inTranscript(receipt);
        if (completed) {
          retainDeliveredInputOutbox(session.id, pending.clientRequestId);
          return {
            ...pending,
            phase: "delivered",
            attachments: [],
            attachmentSummaries:
              pending.attachmentSummaries ??
              summarizeInputAttachments(pending.attachments),
            serverInputId: receipt.id ?? pending.serverInputId,
            serverLiveInputId:
              receipt.live_input_id ?? pending.serverLiveInputId,
            deliveryStatus: receipt.status,
          };
        }

        const activePhase =
          receipt.turn?.is_fresh === true
            ? activeConsoleTurnPhase(turnState)
            : null;
        if (activePhase) {
          return {
            ...pending,
            phase: activePhase,
            serverInputId: receipt.id ?? pending.serverInputId,
            serverLiveInputId:
              receipt.live_input_id ?? pending.serverLiveInputId,
          };
        }
        if (hasUnknownDeliveryError(receipt.last_error)) {
          return {
            ...pending,
            serverInputId: receipt.id ?? pending.serverInputId,
            serverLiveInputId:
              receipt.live_input_id ?? pending.serverLiveInputId,
            phase: "unknown",
            deliveryStatus: receipt.status,
            detail: receipt.last_error,
          };
        }
        if (
          turnState &&
          NONTERMINAL_CONSOLE_TURN_STATES.has(turnState) &&
          receipt.turn?.is_fresh !== true
        ) {
          return {
            ...pending,
            phase: "unknown",
            serverInputId: receipt.id ?? pending.serverInputId,
            serverLiveInputId:
              receipt.live_input_id ?? pending.serverLiveInputId,
            deliveryStatus: receipt.status,
            detail: STALE_CONSOLE_TURN_DETAIL,
          };
        }
        if (stoppedAfterDelivery(receipt)) {
          return {
            ...pending,
            phase: "delivered",
            serverInputId: receipt.id ?? pending.serverInputId,
            serverLiveInputId:
              receipt.live_input_id ?? pending.serverLiveInputId,
          };
        }
        const failed =
          receipt.status === "failed" ||
          receipt.status === "cancelled" ||
          turnState === "failed" ||
          turnState === "cancelled";
        if (failed) {
          return {
            ...pending,
            phase: "failed",
            serverInputId: receipt.id ?? pending.serverInputId,
            serverLiveInputId:
              receipt.live_input_id ?? pending.serverLiveInputId,
            deliveryStatus: receipt.status ?? turnState,
            detail:
              receipt.last_error ||
              (receipt.status === "cancelled" || turnState === "cancelled"
                ? "Input was cancelled before completion."
                : "Input delivery failed."),
          };
        }
        return {
          ...pending,
          serverInputId: receipt.id ?? pending.serverInputId,
          serverLiveInputId:
            receipt.live_input_id ?? pending.serverLiveInputId,
          phase: receipt.status === "queued" ? "queued" : "submitting",
        };
      },
    );
    if (
      nextInputs.some(
        (pending, index) =>
          pending.phase !== pendingManagedLocalInputs[index]?.phase ||
          pending.serverInputId !==
            pendingManagedLocalInputs[index]?.serverInputId ||
          pending.serverLiveInputId !==
            pendingManagedLocalInputs[index]?.serverLiveInputId ||
          pending.detail !== pendingManagedLocalInputs[index]?.detail ||
          pending.deliveryStatus !==
            pendingManagedLocalInputs[index]?.deliveryStatus,
      )
    ) {
      setPendingManagedLocalInputs(nextInputs);
    }
  }, [
    exactInputRows,
    pendingManagedLocalInputs,
    queuedInputsQuery.data,
    session.id,
  ]);
  const pendingInputIds = new Set(
    pendingManagedLocalInputs.map((pending) => pending.clientRequestId),
  );
  const dismissedInputIds = readDismissedInputIds(session.id);
  const activeQueuedInputs = (queuedInputsQuery.data ?? []).filter(
    (row) =>
      row.turn?.origin !== "wake" &&
      !(row.client_request_id && pendingInputIds.has(row.client_request_id)) &&
      !(row.client_request_id && dismissedInputIds.has(row.client_request_id)) &&
      !(row.intent === "steer" && row.last_error === "turn_ended") &&
      !inTranscript(row) &&
      (row.status === "queued" ||
        row.status === "delivering" ||
        hasUnknownDeliveryError(row.last_error)),
  );
  const queueFull =
    activeQueuedInputs.filter((row) => row.status === "queued").length >= 5;
  const failedInputs = (queuedInputsQuery.data ?? []).filter(
    (row) =>
      row.turn?.origin !== "wake" &&
      !(row.client_request_id && pendingInputIds.has(row.client_request_id)) &&
      !(row.client_request_id && dismissedInputIds.has(row.client_request_id)) &&
      (row.status === "failed" || row.status === "cancelled") &&
      !hasUnknownDeliveryError(row.last_error) &&
      !inTranscript(row),
  );
  // Offer an explicit "Queue instead" fallback instead of silently remapping
  // the user's original steer intent.
  const [turnEndedDraft, setTurnEndedDraft] =
    useState<TurnEndedDraft | null>(null);
  const [editingPendingId, setEditingPendingId] = useState<string | null>(null);
  const queueSendUntilHostServes = useCallback(
    (
      message: string,
      intent: "auto" | "queue" | "steer",
      attachments: { blob: Blob; filename: string }[],
      options: ManagedSendOptions,
      clientRequestId: string,
      model: string | null | undefined,
    ): ManagedSendResult => {
      setInputWaitingForHostUpdate(session.id, clientRequestId, true);
      setPendingManagedLocalInputs((current) =>
        current.map((pending) =>
          pending.clientRequestId === clientRequestId
            ? {
                ...pending,
                phase: "submitting",
                detail: HOST_LINK_COPY.sendQueued,
              }
            : pending,
        ),
      );
      setError(null);

      if (!hostResumeRetryWaitersRef.current.has(clientRequestId)) {
        const cancel = hostLinkStore.waitForServing(() => {
          hostResumeRetryWaitersRef.current.set(clientRequestId, null);
          void outboxActionsRef.current.retry(message, intent, attachments, {
            existingClientRequestId: clientRequestId,
            model,
            legacyModelMissing: options.legacyModelMissing,
          });
        });
        hostResumeRetryWaitersRef.current.set(clientRequestId, cancel);
      }

      return { kind: "accepted", clientRequestId };
    },
    [session.id],
  );
  const handleManagedLocalSend = useCallback(
    async (
      message: string,
      intent: "auto" | "queue" | "steer" = "auto",
      attachments: { blob: Blob; filename: string }[] = [],
      options: ManagedSendOptions = {},
    ): Promise<ManagedSendResult> => {
      const clientRequestId =
        options.existingClientRequestId ?? newClientRequestId();
      const model =
        Object.prototype.hasOwnProperty.call(options, "model")
          ? options.model
          : selectedModel.trim() || null;
      // A new send's row appears in the same frame the composer clears; the
      // storage write follows. If the write fails, the row goes and the
      // caller puts the draft back.
      const isNewSend = !options.existingClientRequestId;
      const sentAt = new Date().toISOString();
      const pendingFields = {
        text: message,
        clientRequestId,
        sentAt,
        serverInputId: null,
        serverLiveInputId: null,
        intent,
        model,
        legacyModelMissing: options.legacyModelMissing,
        attachments,
        phase: "submitting" as const,
        detail: options.autoRetryAttempt ? RECONNECTING_DETAIL : null,
      };
      // From here until its POST answers, the send's own answer is coming:
      // its receipt is not polled (the finally around the POST clears this).
      postsInFlightRef.current.add(clientRequestId);
      if (isNewSend) {
        setPendingManagedLocalInputs((current) => [...current, pendingFields]);
      }
      try {
        await persistInputOutbox(session.id, {
          text: message,
          intent,
          clientRequestId,
          model,
          attachments,
        });
        if (hostResumeRetryWaitersRef.current.get(clientRequestId) === null) {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) => {
              if (
                pending.clientRequestId === clientRequestId &&
                pending.detail === HOST_LINK_COPY.sendQueued
              ) {
                return { ...pending, detail: undefined };
              }
              return pending;
            }),
          );
          hostResumeRetryWaitersRef.current.delete(clientRequestId);
        }
      } catch (storageError) {
        const errorMessage =
          storageError instanceof Error
            ? storageError.message
            : "Could not persist input intent before sending";
        postsInFlightRef.current.delete(clientRequestId);
        if (isNewSend) {
          setPendingManagedLocalInputs((current) =>
            current.filter((pending) => pending.clientRequestId !== clientRequestId),
          );
        }
        if (hostResumeRetryWaitersRef.current.get(clientRequestId) === null) {
          hostResumeRetryWaitersRef.current.delete(clientRequestId);
          setInputWaitingForHostUpdate(session.id, clientRequestId, false);
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...pending, phase: "unknown", detail: errorMessage }
                : pending,
            ),
          );
        }
        setError(errorMessage);
        return { kind: "local-persistence-failed", error: errorMessage };
      }

      options.onPersisted?.();
      if (
        options.replacementClientRequestId &&
        options.replacementClientRequestId !== clientRequestId
      ) {
        try {
          dismissInputReceipt(
            session.id,
            options.replacementClientRequestId,
          );
        } catch {
          setError("The replaced failure may remain visible on this device.");
        }
        clearInputOutbox(session.id, options.replacementClientRequestId);
        setPendingManagedLocalInputs((current) =>
          current.filter(
            (pending) =>
              pending.clientRequestId !== options.replacementClientRequestId,
          ),
        );
      }
      const nextPending: PendingManagedLocalInput = pendingFields;
      setPendingManagedLocalInputs((current) => {
        const existing = current.some(
          (pending) => pending.clientRequestId === clientRequestId,
        );
        return existing
          ? current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...nextPending, sentAt: pending.sentAt }
                : pending,
            )
          : [...current, nextPending];
      });
      setIsSubmitting(true);

      const setPendingReceipt = (receipt: QueuedInputSummary): ManagedSendResult => {
        const turnState = receipt.turn?.state;
        const terminalSuccess =
          turnState === "completed" ||
          (!turnState && receipt.status === "delivered") ||
          inTranscript(receipt);
        if (terminalSuccess) {
          markInputDelivered(
            clientRequestId,
            receipt.id,
            receipt.live_input_id,
          );
          if (!turnState) {
            if (sentConfirmationTimerRef.current) {
              clearTimeout(sentConfirmationTimerRef.current);
              sentConfirmationTimerRef.current = null;
            }
            setSentConfirmation(true);
            sentConfirmationTimerRef.current = setTimeout(
              () => setSentConfirmation(false),
              2_000,
            );
          }
          return { kind: "accepted", clientRequestId };
        }
        const activePhase =
          receipt.turn?.is_fresh === true
            ? activeConsoleTurnPhase(turnState)
            : null;
        if (activePhase) {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? {
                    ...pending,
                    phase: activePhase,
                    serverInputId: receipt.id ?? pending.serverInputId,
                    serverLiveInputId:
                      receipt.live_input_id ?? pending.serverLiveInputId,
                  }
                : pending,
            ),
          );
          return { kind: "accepted", clientRequestId };
        }
        if (hasUnknownDeliveryError(receipt.last_error)) {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? {
                    ...pending,
                    phase: "unknown",
                    serverInputId: receipt.id ?? pending.serverInputId,
                    serverLiveInputId:
                      receipt.live_input_id ?? pending.serverLiveInputId,
                    deliveryStatus: receipt.status,
                    detail: receipt.last_error,
                  }
                : pending,
            ),
          );
          setError(UNCONFIRMED_DELIVERY_ERROR);
          return {
            kind: "unknown",
            clientRequestId,
            error: UNCONFIRMED_DELIVERY_ERROR,
          };
        }
        if (stoppedAfterDelivery(receipt)) {
          markInputDelivered(clientRequestId, receipt.id, receipt.live_input_id);
          return { kind: "accepted", clientRequestId };
        }
        const terminalFailure =
          receipt.status === "failed" ||
          receipt.status === "cancelled" ||
          turnState === "failed" ||
          turnState === "cancelled";
        if (terminalFailure) {
          const detail =
            receipt.last_error ||
            (receipt.status === "cancelled" || turnState === "cancelled"
              ? "Input was cancelled before completion."
              : "Input delivery failed.");
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? {
                    ...pending,
                    phase: "failed",
                    serverInputId: receipt.id ?? pending.serverInputId,
                    serverLiveInputId:
                      receipt.live_input_id ?? pending.serverLiveInputId,
                    deliveryStatus: receipt.status ?? turnState,
                    detail,
                  }
                : pending,
            ),
          );
          setError(detail);
          return { kind: "accepted", clientRequestId };
        }
        if (
          turnState &&
          NONTERMINAL_CONSOLE_TURN_STATES.has(turnState) &&
          receipt.turn?.is_fresh !== true
        ) {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? {
                    ...pending,
                    phase: "unknown",
                    serverInputId: receipt.id ?? pending.serverInputId,
                    serverLiveInputId:
                      receipt.live_input_id ?? pending.serverLiveInputId,
                    deliveryStatus: receipt.status,
                    detail: STALE_CONSOLE_TURN_DETAIL,
                  }
                : pending,
            ),
          );
          return { kind: "accepted", clientRequestId };
        }
        const phase: PendingManagedLocalInput["phase"] =
          receipt.status === "queued" ? "queued" : "submitting";
        setPendingManagedLocalInputs((current) =>
          current.map((pending) =>
            pending.clientRequestId === clientRequestId
              ? {
                  ...pending,
                  phase,
                  serverInputId: receipt.id ?? pending.serverInputId,
                  serverLiveInputId:
                    receipt.live_input_id ?? pending.serverLiveInputId,
                  detail: receipt.last_error,
                }
              : pending,
          ),
        );
        return { kind: "accepted", clientRequestId };
      };

      const setRejected = (
        errorMessage: string,
        deliveryStatus?: string | null,
        serverInputId?: number | null,
        serverLiveInputId?: string | null,
      ): ManagedSendResult => {
        setPendingManagedLocalInputs((current) =>
          current.map((pending) =>
            pending.clientRequestId === clientRequestId
              ? {
                  ...pending,
                  phase: "failed",
                  serverInputId: serverInputId ?? pending.serverInputId,
                  serverLiveInputId:
                    serverLiveInputId ?? pending.serverLiveInputId,
                  deliveryStatus: deliveryStatus ?? null,
                  detail: errorMessage,
                }
              : pending,
          ),
        );
        setError(errorMessage);
        return { kind: "rejected", clientRequestId, error: errorMessage };
      };

      try {
        if (
          hostLinkStore.getSnapshot().state === "updating" ||
          hostLinkStore.getSnapshot().state === "slow_update"
        ) {
          return queueSendUntilHostServes(
            message,
            intent,
            attachments,
            options,
            clientRequestId,
            model,
          );
        }
        const result: SessionInputResponse = attachments.length
          ? await postSessionInputMultipart(session.id, {
              text: message,
              attachments,
              intent,
              client_request_id: clientRequestId,
              ...(model ? { model } : {}),
            })
          : await postSessionInput(session.id, {
              text: message,
              intent,
              client_request_id: clientRequestId,
              ...(model ? { model } : {}),
            });
        queryClient.setQueryData<QueuedInputSummary[]>(
          ["session-inputs", session.id],
          result.queued ?? [],
        );
        const receipt = (result.queued ?? []).find(
          (row) => row.client_request_id === clientRequestId,
        );
        if (result.client_request_id && result.client_request_id !== clientRequestId) {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...pending, phase: "unknown" }
                : pending,
            ),
          );
          setError(UNCONFIRMED_DELIVERY_ERROR);
          return { kind: "unknown", clientRequestId, error: UNCONFIRMED_DELIVERY_ERROR };
        }
        const disposition =
          result.disposition ??
          (result.outcome === "unknown" ? "unknown" : "accepted");
        if (disposition === "rejected") {
          return setRejected(
            receipt?.last_error || "Input was rejected before delivery.",
          );
        }
        if (disposition === "unknown") {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...pending, phase: "unknown" }
                : pending,
            ),
          );
          setError(UNCONFIRMED_DELIVERY_ERROR);
          return { kind: "unknown", clientRequestId, error: UNCONFIRMED_DELIVERY_ERROR };
        }

        if (receipt) {
          const accepted = setPendingReceipt({
            ...receipt,
            turn: receipt.turn ?? result.turn,
          });
          if (accepted.kind === "accepted") refreshAfterSend();
          return accepted;
        }
        const responseTurnPhase =
          result.turn?.is_fresh === true
            ? activeConsoleTurnPhase(result.turn.state)
            : null;
        if (responseTurnPhase) {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? {
                    ...pending,
                    phase: responseTurnPhase,
                    serverInputId: result.input_id ?? pending.serverInputId,
                    serverLiveInputId:
                      result.live_input_id ?? pending.serverLiveInputId,
                  }
                : pending,
            ),
          );
          refreshAfterSend();
          return { kind: "accepted", clientRequestId };
        }
        if (
          result.turn?.state &&
          NONTERMINAL_CONSOLE_TURN_STATES.has(result.turn.state) &&
          result.turn.is_fresh !== true
        ) {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? {
                    ...pending,
                    phase: "unknown",
                    serverInputId: result.input_id ?? pending.serverInputId,
                    serverLiveInputId:
                      result.live_input_id ?? pending.serverLiveInputId,
                    detail: STALE_CONSOLE_TURN_DETAIL,
                  }
                : pending,
            ),
          );
          refreshAfterSend();
          return { kind: "accepted", clientRequestId };
        }
        if (result.outcome === "sent") {
          markInputDelivered(
            clientRequestId,
            result.input_id,
            result.live_input_id,
          );
          queryClient.setQueryData<SessionLockInfo | null>(
            ["session-lock", session.id],
            {
              locked: true,
              holder: null,
              time_remaining_seconds: null,
              fork_available: true,
            },
          );
          if (sentConfirmationTimerRef.current)
            clearTimeout(sentConfirmationTimerRef.current);
          setSentConfirmation(true);
          sentConfirmationTimerRef.current = setTimeout(
            () => setSentConfirmation(false),
            2_000,
          );
          refreshAfterSend();
          return { kind: "accepted", clientRequestId };
        }
        if (disposition === "accepted" && result.outcome === "unknown") {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...pending, phase: "submitting" }
                : pending,
            ),
          );
          void queryClient.invalidateQueries({
            queryKey: ["session-input", session.id, clientRequestId],
          });
          return { kind: "accepted", clientRequestId };
        }
        setError(UNCONFIRMED_DELIVERY_ERROR);
        setPendingManagedLocalInputs((current) =>
          current.map((pending) =>
            pending.clientRequestId === clientRequestId
              ? { ...pending, phase: "unknown" }
              : pending,
          ),
        );
        return { kind: "unknown", clientRequestId, error: UNCONFIRMED_DELIVERY_ERROR };
      } catch (error) {
        const apiError =
          error && typeof error === "object"
            ? (error as { status?: unknown; body?: unknown })
            : null;
        const apiBody =
          apiError?.body && typeof apiError.body === "object"
            ? (apiError.body as { code?: unknown })
            : null;
        if (apiError?.status === 503 && apiBody?.code === "runtime_restarting") {
          return queueSendUntilHostServes(
            message,
            intent,
            attachments,
            options,
            clientRequestId,
            model,
          );
        }
        const structured = structuredInputErrorDetail(error);
        if (
          structured?.client_request_id &&
          structured.client_request_id !== clientRequestId
        ) {
          setError(UNCONFIRMED_DELIVERY_ERROR);
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...pending, phase: "unknown" }
                : pending,
            ),
          );
          return {
            kind: "unknown",
            clientRequestId,
            error: UNCONFIRMED_DELIVERY_ERROR,
          };
        }
        if (
          structured?.error_code === "idempotency_conflict" ||
          structured?.error_code === "input_conflict"
        ) {
          // The request ID already belongs to a different payload. Its receipt
          // confirms that earlier input, not this one, so this send failed.
          return setRejected(IDEMPOTENCY_CONFLICT_ERROR);
        }
        let exactReceipt: QueuedInputSummary | null = null;
        try {
          exactReceipt = await fetchSessionInput(session.id, clientRequestId);
          queryClient.setQueryData(
            ["session-input", session.id, clientRequestId],
            exactReceipt,
          );
        } catch {
          // The structured error or unknown outcome remains authoritative.
        }
        const messageFromServer =
          structured?.message ||
          (error instanceof Error ? error.message : "Input request failed.");
        if (intent === "steer" && structured?.error_code === "turn_ended") {
          setTurnEndedDraft({ clientRequestId, text: message, model: model ?? null });
          return setRejected(
            messageFromServer,
            exactReceipt?.status ?? structured.delivery_status,
            exactReceipt?.id ?? structured.input_id,
            exactReceipt?.live_input_id ?? structured.live_input_id,
          );
        }
        if (exactReceipt) return setPendingReceipt(exactReceipt);
        if (structured?.disposition === "rejected") {
          return setRejected(messageFromServer, structured.delivery_status);
        }
        if (structured?.disposition === "accepted") {
          const errorTurnPhase =
            structured.turn?.is_fresh === true
              ? activeConsoleTurnPhase(structured.turn.state)
              : null;
          if (errorTurnPhase) {
            setPendingManagedLocalInputs((current) =>
              current.map((pending) =>
                pending.clientRequestId === clientRequestId
                  ? {
                      ...pending,
                      phase: errorTurnPhase,
                      serverInputId:
                        structured.input_id ?? pending.serverInputId,
                      serverLiveInputId:
                        structured.live_input_id ?? pending.serverLiveInputId,
                    }
                  : pending,
              ),
            );
            return { kind: "accepted", clientRequestId };
          }
          const errorTurnState = structured.turn?.state.toLowerCase();
          if (
            structured.delivery_status === "cancelled" ||
            errorTurnState === "cancelled"
          ) {
            setRejected(
              "Input was cancelled before completion.",
              structured.delivery_status ?? errorTurnState,
              structured.input_id,
              structured.live_input_id,
            );
            return { kind: "accepted", clientRequestId };
          }
          const terminalFailure =
            !hasUnknownDeliveryError(structured.error_code) &&
            (structured.delivery_status === "failed" ||
              errorTurnState === "failed");
          if (terminalFailure) {
            setPendingManagedLocalInputs((current) =>
              current.map((pending) =>
                pending.clientRequestId === clientRequestId
                  ? {
                      ...pending,
                      phase: "failed",
                      serverInputId:
                        structured.input_id ?? pending.serverInputId,
                      serverLiveInputId:
                        structured.live_input_id ?? pending.serverLiveInputId,
                      deliveryStatus:
                        structured.delivery_status ?? errorTurnState,
                      detail: messageFromServer,
                    }
                  : pending,
              ),
            );
            setError(messageFromServer);
            return { kind: "accepted", clientRequestId };
          }
        }
        const attempt = options.autoRetryAttempt ?? 0;
        if (
          isTransientSendFailure(error, structured) &&
          attempt < AUTO_RETRY_DELAYS_MS.length
        ) {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...pending, phase: "submitting", detail: RECONNECTING_DETAIL }
                : pending,
            ),
          );
          const timer = setTimeout(() => {
            autoRetryTimersRef.current.delete(timer);
            void outboxActionsRef.current.retry(message, intent, attachments, {
              existingClientRequestId: clientRequestId,
              model,
              legacyModelMissing: options.legacyModelMissing,
              autoRetryAttempt: attempt + 1,
            });
          }, AUTO_RETRY_DELAYS_MS[attempt]);
          autoRetryTimersRef.current.add(timer);
          return { kind: "accepted", clientRequestId };
        }
        setPendingManagedLocalInputs((current) =>
          current.map((pending) =>
            pending.clientRequestId === clientRequestId
              ? { ...pending, phase: "unknown", detail: messageFromServer }
              : pending,
          ),
        );
        setError(UNCONFIRMED_DELIVERY_ERROR);
        return { kind: "unknown", clientRequestId, error: UNCONFIRMED_DELIVERY_ERROR };
      } finally {
        postsInFlightRef.current.delete(clientRequestId);
        setIsSubmitting(false);
      }
    },
    [
      markInputDelivered,
      queryClient,
      refreshAfterSend,
      queueSendUntilHostServes,
      selectedModel,
      session.id,
    ],
  );

  const handleCancelQueuedInput = useCallback(
    async (input: QueuedInputSummary) => {
      try {
        await cancelSessionInput(session.id, input);
        const clientRequestId = input.client_request_id;
        if (clientRequestId) {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? {
                    ...pending,
                    detail: "Cancellation requested; awaiting confirmation.",
                  }
                : pending,
            ),
          );
          void queryClient.invalidateQueries({
            queryKey: ["session-input", session.id, clientRequestId],
          });
        }
        void queuedInputsQuery.refetch();
      } catch (e) {
        setError(
          e instanceof Error ? e.message : "Could not cancel queued input",
        );
      }
    },
    [queryClient, queuedInputsQuery, session.id],
  );

  const handleInterrupt = useCallback(async () => {
    if (
      !isManagedLocal ||
      isInterrupting ||
      session.session_state.control.actions.interrupt.state !== "available"
    )
      return;
    setIsInterrupting(true);
    setError(null);
    try {
      await (session.session_state.mode === "console"
        ? interruptConsoleTurn(session.id)
        : interruptLiveSession(session.id));
      queryClient.setQueryData<SessionLockInfo | null>(
        ["session-lock", session.id],
        {
          locked: false,
          holder: null,
          time_remaining_seconds: null,
          fork_available: false,
        },
      );
      await refreshCurrentSessionWorkspace();
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Could not interrupt running turn",
      );
    } finally {
      setIsInterrupting(false);
    }
  }, [
    isInterrupting,
    isManagedLocal,
    queryClient,
    refreshCurrentSessionWorkspace,
    session.id,
    session.session_state,
  ]);

  const canSteerNow = isSendLocked && canSteerActiveTurn;
  const canQueueNow = isSendLocked && canQueueNextInput && !queueFull;
  const canInterruptTurn =
    isManagedLocal &&
    session.session_state.control.actions.interrupt.state === "available";
  const parkedBackgroundWork =
    session.session_state.mode === "console" &&
    session.session_state.run?.lifecycle === "ended" &&
    session.session_state.delegation?.state === "pending" &&
    (session.session_state.delegation.count ?? 0) > 0;
  const interruptActionLabel = parkedBackgroundWork ? "Stop background work" : "Stop";
  // Images ride whichever intent the action uses (send, steer or queue).
  const attachmentInputEnabled = attachImagesEnabled;
  // A parked Console invocation has no active turn, but its provider-owned
  // work is still live and the served interrupt action closes that invocation.
  const showInlineInterrupt =
    canInterruptTurn && !isStalled && (isSendLocked || parkedBackgroundWork);
  // When steer is available, the primary action is steer. Queue-next becomes
  // a secondary escape hatch. If only queue is available, primary = queue.
  const primaryIntent: "auto" | "queue" | "steer" = !isSendLocked
    ? "auto"
    : canSteerNow
      ? "steer"
      : canQueueNow
        ? "queue"
        : "auto";
  // Primary send is blocked when there's no available action.
  const isSendBlocked = isSendLocked && !canSteerNow && !canQueueNow;
  // Attachment-only sends and queues are valid.
  const hasComposerContent =
    Boolean(draft.trim()) || composerAttachments.attachments.length > 0;

  const handleSend = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();

      const message = draft.trim();
      const pendingAttachments = composerAttachments.attachments;
      const hasAttachments = pendingAttachments.length > 0;
      if (!message && !hasAttachments) return;
      if (isSubmitting || isComposerDisabled || isSendBlocked) return;
      if (composerAttachments.isCompressing) return;

      setDraft("");
      setError(null);
      setBlockedKeyboardSubmit(false);
      const attachmentArgs = pendingAttachments.map((attachment) => ({
        blob: attachment.blob,
        filename: attachment.filename,
      }));
      const replacementClientRequestId = editingPendingId;
      const result = await handleManagedLocalSend(
        message,
        primaryIntent,
        attachmentArgs,
        {
          replacementClientRequestId: replacementClientRequestId ?? undefined,
          onPersisted: () => {
            composerAttachments.clear();
            setEditingPendingId(null);
          },
        },
      );
      if (result.kind === "local-persistence-failed") {
        setDraft(message);
      }
    },
    [
      composerAttachments,
      draft,
      editingPendingId,
      handleManagedLocalSend,
      isComposerDisabled,
      isSendBlocked,
      isSubmitting,
      primaryIntent,
    ],
  );

  const handleComposerPaste = useCallback(
    (e: React.ClipboardEvent) => {
      if (!attachmentInputEnabled) return;
      const files: File[] = [];
      for (const item of Array.from(e.clipboardData.items)) {
        if (item.kind === "file" && item.type.startsWith("image/")) {
          const f = item.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length) {
        e.preventDefault();
        void composerAttachments.addFiles(files);
      }
    },
    [attachmentInputEnabled, composerAttachments],
  );

  const handleComposerDrop = useCallback(
    (e: React.DragEvent) => {
      if (!attachmentInputEnabled) return;
      const dropped = Array.from(e.dataTransfer.files);
      if (!dropped.length) return;
      // Always preventDefault on file drops — even non-image drops, otherwise
      // the browser navigates away and the user loses their draft.
      e.preventDefault();
      const images = dropped.filter((f) => f.type.startsWith("image/"));
      if (images.length) void composerAttachments.addFiles(images);
    },
    [attachmentInputEnabled, composerAttachments],
  );

  const handleComposerDragOver = useCallback(
    (e: React.DragEvent) => {
      if (!attachmentInputEnabled) return;
      if (Array.from(e.dataTransfer.items).some((it) => it.kind === "file")) {
        e.preventDefault();
      }
    },
    [attachmentInputEnabled],
  );

  const handleSecondaryQueue = useCallback(async () => {
    if (isSubmitting || isComposerDisabled || !canQueueNow) return;
    if (composerAttachments.isCompressing) return;
    const message = draft.trim();
    const pendingAttachments = composerAttachments.attachments;
    if (!message && pendingAttachments.length === 0) return;
    setDraft("");
    setError(null);
    const attachmentArgs = pendingAttachments.map((attachment) => ({
      blob: attachment.blob,
      filename: attachment.filename,
    }));
    const result = await handleManagedLocalSend(message, "queue", attachmentArgs, {
      replacementClientRequestId: editingPendingId ?? undefined,
      onPersisted: () => {
        composerAttachments.clear();
        setEditingPendingId(null);
      },
    });
    if (result.kind === "local-persistence-failed") setDraft(message);
  }, [
    composerAttachments,
    draft,
    editingPendingId,
    isSubmitting,
    isComposerDisabled,
    canQueueNow,
    handleManagedLocalSend,
  ]);

  const handleQueueInsteadAfterTurnEnded = useCallback(async () => {
    const draft = turnEndedDraft;
    if (!draft) return;
    setError(null);
    await handleManagedLocalSend(draft.text, "queue", [], {
      model: draft.model,
      replacementClientRequestId: draft.clientRequestId,
      onPersisted: () =>
        setTurnEndedDraft((current) =>
          current?.clientRequestId === draft.clientRequestId ? null : current,
        ),
    });
  }, [turnEndedDraft, handleManagedLocalSend]);

  const handleDiscardPending = useCallback(
    (clientRequestId: string) => {
      hostResumeRetryWaitersRef.current.get(clientRequestId)?.();
      hostResumeRetryWaitersRef.current.delete(clientRequestId);
      try {
        dismissInputReceipt(session.id, clientRequestId);
      } catch {
        setError("Could not save this dismissal; the input remains available.");
        return;
      }
      clearInputOutbox(session.id, clientRequestId);
      setPendingManagedLocalInputs((current) =>
        current.filter((pending) => pending.clientRequestId !== clientRequestId),
      );
      setEditingPendingId((current) =>
        current === clientRequestId ? null : current,
      );
      void queryClient.removeQueries({
        queryKey: ["session-input", session.id, clientRequestId],
      });
    },
    [queryClient, session.id],
  );

  const handleEditPending = useCallback(
    (pending: PendingManagedLocalInput) => {
      hostResumeRetryWaitersRef.current.get(pending.clientRequestId)?.();
      hostResumeRetryWaitersRef.current.delete(pending.clientRequestId);
      setInputWaitingForHostUpdate(session.id, pending.clientRequestId, false);
      setEditingPendingId(pending.clientRequestId);
      setDraft(pending.text);
      if (!pending.legacyModelMissing) {
        handleSelectedModelChange(pending.model ?? "");
      }
      composerAttachments.restore(pending.attachments);
      setError(null);
    },
    [composerAttachments, handleSelectedModelChange, session.id],
  );

  // Actions go through a ref so the reported entries only change when what
  // the user sees changes, not whenever a handler closure is rebuilt.
  const outboxActionsRef = useRef({
    retry: handleManagedLocalSend,
    cancel: handleCancelQueuedInput,
    queueInstead: handleQueueInsteadAfterTurnEnded,
    edit: handleEditPending,
    discard: handleDiscardPending,
  });
  outboxActionsRef.current = {
    retry: handleManagedLocalSend,
    cancel: handleCancelQueuedInput,
    queueInstead: handleQueueInsteadAfterTurnEnded,
    edit: handleEditPending,
    discard: handleDiscardPending,
  };
  useEffect(() => {
    for (const pending of pendingManagedLocalInputs) {
      if (
        pending.phase !== "submitting" ||
        pending.detail !== HOST_LINK_COPY.sendQueued ||
        hostResumeRetryWaitersRef.current.has(pending.clientRequestId)
      ) {
        continue;
      }
      const cancel = hostLinkStore.waitForServing(() => {
        hostResumeRetryWaitersRef.current.set(pending.clientRequestId, null);
        void outboxActionsRef.current.retry(
          pending.text,
          pending.intent,
          pending.attachments,
          {
            existingClientRequestId: pending.clientRequestId,
            model: pending.model,
            legacyModelMissing: pending.legacyModelMissing,
          },
        );
      });
      hostResumeRetryWaitersRef.current.set(pending.clientRequestId, cancel);
    }
  }, [pendingManagedLocalInputs]);
  // The listed receipts, with a send's own receipt in place of its listed
  // copy where that is fresher (exactInputRows holds only those).
  const receiptRows = useMemo<QueuedInputSummary[]>(() => {
    const listed = queuedInputsQuery.data ?? [];
    const merged = listed.map((row) =>
      (row.client_request_id ? exactInputRows.get(row.client_request_id) : undefined) ?? row,
    );
    const listedIds = new Set(listed.map((row) => row.client_request_id));
    for (const [clientRequestId, row] of exactInputRows) {
      if (!listedIds.has(clientRequestId)) merged.push(row);
    }
    return merged;
  }, [exactInputRows, queuedInputsQuery.data]);
  // Re-render when a linked receipt's bridge window closes.
  const [bridgeClock, setBridgeClock] = useState(0);
  const firstSeenLinkedAtRef = useRef(new Map<string, number>());
  useEffect(() => {
    const now = Date.now();
    const next = Math.min(
      ...receiptRows
        .map((row) => linkedReceiptBridgeEndsAt(row, firstSeenLinkedAtRef.current, now))
        .filter((at): at is number => at != null && at > now),
    );
    if (!Number.isFinite(next)) return;
    const timer = setTimeout(() => setBridgeClock((tick) => tick + 1), next - now + 50);
    return () => clearTimeout(timer);
  }, [bridgeClock, receiptRows]);
  const outboxEntries = useMemo<OutboxEntry[]>(() => {
    if (!ownsManagedInputs) return [];
    const rows = receiptRows;
    const now = Date.now();
    const canEditInComposer = isManagedLocal && !isComposerDisabled;
    const copyAction = (text: string) => ({
      label: "Copy",
      onClick: () => void copyToClipboard(text),
    });
    const receiptFor = (clientRequestId: string) =>
      rows.find((row) => row.client_request_id === clientRequestId);
    const pendingIds = new Set(
      pendingManagedLocalInputs.map((pending) => pending.clientRequestId),
    );
    const cancelAction = (row: QueuedInputSummary) => ({
      label: "Cancel",
      onClick: () => void outboxActionsRef.current.cancel(row),
    });
    const failed: OutboxEntry[] = [];
    const inFlight: OutboxEntry[] = [];
    const queued: OutboxEntry[] = [];
    const placed: OutboxEntry[] = [];

    const dismissedReceipts = readDismissedInputIds(session.id);
    const shownByTranscript = receiptsShownInTranscript(rows);
    for (const row of rows) {
      const clientRequestId = row.client_request_id;
      // A linked receipt the loaded rows do not show yet stays, so the
      // user's row never blanks between the link and the transcript read.
      const bridgeEndsAt = linkedReceiptBridgeEndsAt(row, firstSeenLinkedAtRef.current, now);
      if (
        (inTranscript(row) && !(bridgeEndsAt != null && now < bridgeEndsAt)) ||
        shownByTranscript.has(row) ||
        (clientRequestId &&
          (pendingIds.has(clientRequestId) || dismissedReceipts.has(clientRequestId)))
      ) {
        continue;
      }
      const key = `receipt:${clientRequestId ?? row.live_input_id ?? row.id ?? "unknown"}`;
      const turnState = row.turn?.state;
      const attachments = (row.attachments ?? []).map((attachment) => ({
        filename: attachment.filename,
        mimeType: attachment.mime_type,
        byteSize: attachment.byte_size,
      }));
      const origin =
        row.origin === "wake" || row.origin === "longhouse"
          ? row.origin
          : row.turn?.origin === "wake"
            ? "wake"
            : "user";
      if (origin !== "user") {
        inFlight.push({
          key,
          clientRequestId,
          sentAt: row.created_at ?? null,
          text: row.text,
          attachments,
          state: "sent",
          origin,
        });
        continue;
      }
      if (hasUnknownDeliveryError(row.last_error)) {
        const unconfirmedActions = [
          ...(row.text.trim().length > 0 ? [copyAction(row.text)] : []),
          ...(row.status === "failed" && clientRequestId
            ? [
                {
                  label: "Dismiss",
                  onClick: () =>
                    outboxActionsRef.current.discard(clientRequestId),
                },
              ]
            : []),
        ];
        inFlight.push({
          key,
          clientRequestId,
          sentAt: row.created_at ?? null,
          text: row.text,
          attachments,
          state: "unconfirmed",
          detail: hasRuntimeDrainingError(row.last_error)
            ? "runtime restarting"
            : row.last_error,
          actions:
            unconfirmedActions.length > 0 ? unconfirmedActions : undefined,
        });
      } else if (
        turnState &&
        NONTERMINAL_CONSOLE_TURN_STATES.has(turnState) &&
        row.turn?.is_fresh !== true
      ) {
        inFlight.push({
          key,
          clientRequestId,
          sentAt: row.created_at ?? null,
          text: row.text,
          attachments,
          state: row.status === "delivered" ? "sent" : "unconfirmed",
          detail: STALE_CONSOLE_TURN_DETAIL,
        });
      } else if (turnState === "queued") {
        queued.push({
          key,
          clientRequestId,
          sentAt: row.created_at ?? null,
          text: row.text,
          attachments,
          state: "queued",
          actions: [cancelAction(row)],
        });
      } else if (
        turnState &&
        NONTERMINAL_CONSOLE_TURN_STATES.has(turnState)
      ) {
        inFlight.push({ key, clientRequestId, sentAt: row.created_at ?? null, text: row.text, attachments, state: "sent" });
      } else if (row.status === "delivering") {
        inFlight.push({ key, clientRequestId, sentAt: row.created_at ?? null, text: row.text, attachments, state: "sending" });
      } else if (row.status === "queued") {
        queued.push({
          key,
          clientRequestId,
          sentAt: row.created_at ?? null,
          text: row.text,
          attachments,
          state: "queued",
          actions: [cancelAction(row)],
        });
      } else if (isSettledDelivery(row) && row.created_at) {
        // Delivered, and the transcript does not show it: a steer the
        // provider never wrote down, a send from another client, or one its
        // run lost. The receipt is the only record, so it stands at the time
        // it was sent, among the transcript rows, for every client.
        const lost = failedBeforeRecorded(row);
        placed.push({
          key,
          clientRequestId,
          sentAt: row.created_at ?? null,
          text: row.text,
          attachments,
          at: row.created_at,
          state: lost ? "failed" : "sent",
          detail: lost ? "the run ended before the agent read it" : null,
          actions:
            lost && clientRequestId
              ? [
                  {
                    label: "Dismiss",
                    onClick: () =>
                      outboxActionsRef.current.discard(clientRequestId),
                  },
                ]
              : undefined,
        });
      } else if (
        row.status === "failed" ||
        (row.status === "cancelled" && row.last_error)
      ) {
        const failedReceiptActions = [
          ...(!canEditInComposer && row.text.trim().length > 0
            ? [copyAction(row.text)]
            : []),
          ...(clientRequestId
            ? [
                {
                  label: "Dismiss",
                  onClick: () =>
                    outboxActionsRef.current.discard(clientRequestId),
                },
              ]
            : []),
        ];
        failed.push({
          key,
          clientRequestId,
          sentAt: row.created_at ?? null,
          text: row.text,
          attachments,
          state: "failed",
          detail: row.last_error || "Input was cancelled before completion.",
          actions:
            failedReceiptActions.length > 0 ? failedReceiptActions : undefined,
        });
      }
    }

    for (const pending of pendingManagedLocalInputs) {
      const key = `pending:${pending.clientRequestId}`;
      const receipt = receiptFor(pending.clientRequestId);
      const base = {
        key,
        clientRequestId: pending.clientRequestId,
        sentAt: pending.sentAt,
        text: pending.text,
        attachments:
          pending.attachmentSummaries ??
          summarizeInputAttachments(pending.attachments),
        warning: pending.legacyModelMissing ? LEGACY_MODEL_WARNING : null,
      };
      if (pending.phase === "delivered") {
        inFlight.push({ ...base, state: "sent" });
      } else if (
        pending.phase === "unknown" &&
        pending.deliveryStatus === "delivered" &&
        pending.detail === STALE_CONSOLE_TURN_DETAIL
      ) {
        inFlight.push({ ...base, state: "sent", detail: pending.detail });
      } else if (pending.phase === "unknown") {
        const unknownActions = [
          ...(pending.attachmentsLost
            ? []
            : [
                {
                  label: "Retry",
                  disabled: isSubmitting,
                  onClick: () =>
                    void outboxActionsRef.current.retry(
                      pending.text,
                      pending.intent,
                      pending.attachments,
                      {
                        existingClientRequestId: pending.clientRequestId,
                        model: pending.model,
                        legacyModelMissing: pending.legacyModelMissing,
                      },
                    ),
                },
              ]),
          ...(!canEditInComposer && pending.text.trim().length > 0
            ? [copyAction(pending.text)]
            : []),
        ];
        inFlight.push({
          ...base,
          state: "unconfirmed",
          detail: pending.detail || UNCONFIRMED_DELIVERY_ERROR,
          actions: unknownActions,
        });
      } else if (pending.phase === "failed") {
        if (turnEndedDraft?.clientRequestId === pending.clientRequestId) {
          continue;
        }
        failed.push({
          ...base,
          state: "failed",
          detail: pending.detail || "Input delivery failed.",
          actions: [
            ...(canEditInComposer
              ? [
                  {
                    label: "Edit",
                    onClick: () => outboxActionsRef.current.edit(pending),
                  },
                ]
              : pending.text.trim().length > 0
                ? [copyAction(pending.text)]
                : []),
            {
              label: "Discard",
              onClick: () =>
                outboxActionsRef.current.discard(pending.clientRequestId),
            },
          ],
        });
      } else if (pending.phase === "queued") {
        queued.push({
          ...base,
          state: "queued",
          actions: receipt ? [cancelAction(receipt)] : undefined,
        });
      } else if (ACCEPTED_INPUT_PHASES.has(pending.phase)) {
        inFlight.push({ ...base, state: "sent" });
      } else {
        inFlight.push({
          ...base,
          state: "sending",
          detail:
            pending.detail === HOST_LINK_COPY.sendQueued
              ? null
              : pending.detail ?? null,
        });
      }
    }

    if (turnEndedDraft) {
      failed.push({
        key: `turn-ended:${turnEndedDraft.clientRequestId}`,
        text: turnEndedDraft.text,
        state: "failed",
        detail: "the turn ended before it arrived",
        actions: [
          {
            label: "Queue instead",
            onClick: () => void outboxActionsRef.current.queueInstead(),
          },
          { label: "Dismiss", onClick: () => setTurnEndedDraft(null) },
        ],
      });
    }
    return [...placed, ...failed, ...inFlight, ...queued];
    // bridgeClock: recompute when a linked receipt's bridge window closes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    bridgeClock,
    isComposerDisabled,
    isManagedLocal,
    isSubmitting,
    ownsManagedInputs,
    pendingManagedLocalInputs,
    receiptRows,
    receiptsShownInTranscript,
    session.id,
    timelineItems,
    turnEndedDraft,
  ]);
  // Before paint, so the transcript shows a send in the same frame the
  // composer clears instead of one frame later.
  useLayoutEffect(() => {
    onOutboxChange?.(outboxEntries);
  }, [onOutboxChange, outboxEntries]);
  useEffect(() => {
    if (!onOutboxChange) return;
    return () => onOutboxChange([]);
  }, [onOutboxChange]);
  // The transcript row already says "Not confirmed" / "Not delivered" with
  // its own Retry or Queue-instead action; repeating it above the composer
  // is noise.
  const errorShownInTranscript =
    outboxInTranscript &&
    Boolean(error) &&
    (Boolean(turnEndedDraft) ||
      (error?.endsWith(UNCONFIRMED_DELIVERY_ERROR) ?? false));

  const hostUpdateQueued = pendingManagedLocalInputs.some(
    (pending) =>
      pending.phase === "submitting" &&
      pending.detail === HOST_LINK_COPY.sendQueued,
  );
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (requireClickForFirstSend && draft.trim() && !blockedKeyboardSubmit) {
        setBlockedKeyboardSubmit(true);
        return;
      }
      handleSend(e as unknown as FormEvent);
      return;
    }
    if (
      e.key === "Tab" &&
      !e.shiftKey &&
      !e.altKey &&
      !e.ctrlKey &&
      !e.metaKey &&
      !e.nativeEvent.isComposing &&
      canQueueNow &&
      (draft.trim() || composerAttachments.attachments.length > 0)
    ) {
      e.preventDefault();
      void handleSecondaryQueue();
    }
  };

  const submitButtonLabel = !isSendLocked
    ? submitLabel
    : canSteerNow
      ? "Send"
      : canQueueNow
        ? "Queue"
        : queueFull
          ? "Queue full"
          : "Waiting";
  const turnNoticeText = canSteerNow
    ? "Enter sends this into the running turn. Tab queues it for after this turn."
    : "Enter queues this for after the running turn.";

  // Composer status line: ember + the server's working label + a mono timer
  // while a turn is active, ember + the server's attention copy when a
  // provider question is pending; at rest on an open session it folds into
  // the placeholder (a closed one keeps the caller's). Shares its
  // tone read with the session header (sessionHeaderState.ts) so the two
  // never disagree about live/attention/cool, but keeps its own mono clock
  // timer rather than a word-based duration, matching the instrument
  // panel's Nixie-style readout (Phase 4 wraps it in a capsule).
  //
  // `turnStartMs` is the same shared turn-elapsed anchor the session header
  // and the readout rail use (getRunningTurnStartMs) — derived from the same
  // durable timeline rows passed in as `timelineItems`, so this clock can
  // never drift from theirs.
  const turnStartMs = useMemo(
    () => getRunningTurnStartMs(timelineItems ?? []),
    [timelineItems],
  );
  const composerState = getSessionHeaderState(
    session,
    activityNowMs,
    turnStartMs,
  );
  const isDelegatedWork =
    composerState.tone === "live" &&
    delegatedWorkLabel(session.session_state, activityNowMs) !== null;
  const composerElapsedSeconds =
    composerState.tone === "live" && !isDelegatedWork && turnStartMs != null
      ? Math.max(0, Math.floor((activityNowMs - turnStartMs) / 1_000))
      : null;
  // The server's label, verbatim, exactly as the session header shows it. It
  // only renders while the header's freshness gate calls the session live.
  const composerWorkingLabel = workingStatusLabel(session.session_state);
  const runningToolInfo = useMemo(
    () => findRunningTool(timelineItems ?? []),
    [timelineItems],
  );
  const composerForegroundDetail =
    composerState.tone === "live" &&
    runningToolInfo &&
    runningToolInfo.label &&
    runningToolInfo.label !== runningToolInfo.toolName
      ? runningToolInfo.label
      : null;
  const hasBackgroundRoster =
    (session.session_state?.delegation?.items?.length ?? 0) > 0 ||
    (session.pending_background_agent_count ?? 0) > 0;
  const composerDelegatedDetail =
    composerState.tone === "live" && !hasBackgroundRoster
      ? delegatedWorkDetailFromFacts(session.session_state, activityNowMs)
      : null;
  const composerHeadDetail = composerForegroundDetail || composerDelegatedDetail;
  const leafRef = useRef<HTMLSpanElement | null>(null);
  const leafAnimationRef = useRef<Animation | null>(null);
  const leafLive = composerState.tone === "live" && !isDelegatedWork;

  useLayoutEffect(() => {
    const node = leafRef.current;
    if (!node) return;
    leafAnimationRef.current?.cancel();
    leafAnimationRef.current = null;
    if (!leafLive) {
      node.style.opacity = "0";
      node.style.boxShadow = "none";
      return;
    }
    const reduceMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduceMotion) {
      node.style.opacity = "0.75";
      node.style.boxShadow = "0 0 8px rgba(240, 138, 36, 0.45)";
      return;
    }
    node.style.opacity = "0.22";
    // Static glow stays off the main-thread repaint path; WAAPI animates opacity only.
    node.style.boxShadow = "0 0 10px 1px rgba(240, 138, 36, 0.55)";
    const unsubscribe = activityFeed?.subscribe((frame) => {
      if (!frame || frame.kind === "state") return;
      if (getWindowActivity() !== "focused") return;
      leafAnimationRef.current?.cancel();
      if (typeof node.animate !== "function") return;
      leafAnimationRef.current = node.animate(
        [
          { opacity: 0.22 },
          { opacity: 1, offset: 0.14 },
          { opacity: 0.22 },
        ],
        {
          duration: 550,
          easing: "cubic-bezier(0.2, 0.8, 0.2, 1)",
        },
      );
    });
    return () => {
      unsubscribe?.();
      leafAnimationRef.current?.cancel();
      leafAnimationRef.current = null;
    };
  }, [activityFeed, leafLive]);
  const composerObservedClock = formatClockTime(Date.parse(activity.observed_at ?? ""));
  // Dock: the status line shows only while something runs or needs a
  // decision; at rest on an open session the placeholder carries it ("Idle
  // since 2:21 AM — message to continue"). When BackgroundTaskRoster already
  // summarizes background work just above the input, avoid repeating
  // "Background · N commands" inside the placeholder.
  const composerHeadVisible =
    showComposerUnavailableState || composerState.tone !== "cool";
  // Only an open, resting session continues from here; an ended one that
  // still accepts input keeps the caller's own placeholder.
  const dockPlaceholder =
    composerState.tone === "cool" && !isSessionClosed(session)
      ? hasBackgroundRoster && composerState.text.startsWith("Background")
        ? "Message to continue"
        : `${composerState.text} — message to continue`
      : composerPlaceholder || "Message";

  // Renders inside the composer frame itself, between the head row and the
  // input (see below) — one framed object, not a separate block floating
  // above it.
  const queuedBanner =
    ownsManagedInputs && !outboxInTranscript && activeQueuedInputs.length > 0 ? (
      <div className="session-chat-queued" data-testid="session-chat-queued">
        <div className="session-chat-queued__label">
          {activeQueuedInputs.some((row) =>
            hasRuntimeDrainingError(row.last_error)
          )
            ? "Runtime restarting — retry with same request"
            : activeQueuedInputs.some(
                (row) =>
                  row.status === "delivering" ||
                  hasProviderDeliveryUnknownError(row.last_error),
              )
              ? "Delivery status uncertain"
              : "Queued, sends next"}
        </div>
        <ul className="session-chat-queued__list">
          {activeQueuedInputs.map((row) => (
            <li
              key={
                row.client_request_id ?? row.live_input_id ?? row.id ?? row.text
              }
              className="session-chat-queued__item"
            >
              <span className="session-chat-queued__text">{row.text}</span>
              <span
                className={`session-chat-queued__status session-chat-queued__status--${row.status}`}
              >
                {row.status === "delivering" ||
                hasUnknownDeliveryError(row.last_error)
                  ? row.last_error ||
                    "Not confirmed — retry with the same request"
                  : "Queued"}
              </span>
              {row.status === "queued" ? (
                <button
                  type="button"
                  className="session-chat-queued__cancel"
                  onClick={() => void handleCancelQueuedInput(row)}
                  aria-label="Cancel queued message"
                >
                  Cancel
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      </div>
    ) : null;

  // Console sessions pick a model per turn, so their dock composer is two
  // rows: the text, then a toolbar holding attach, the model chip and the
  // actions. Helm keeps the one-row composer; a second row appears only for a
  // control the user can change.
  const hasComposerToolbar = session.session_state.mode === "console";
  const composerAttachButton = attachImagesEnabled ? (
    <AttachmentTray
      showThumbs={false}
      attachments={composerAttachments.attachments}
      onAddFiles={composerAttachments.addFiles}
      onRemove={composerAttachments.removeAttachment}
      isCompressing={composerAttachments.isCompressing}
      disabled={isSubmitting}
      addDisabled={!attachmentInputEnabled}
    />
  ) : null;
  const stopInterruptButton = showInlineInterrupt ? (
    <Button
      type="button"
      variant="danger"
      size="sm"
      className="session-chat-btn session-chat-btn--stop"
      aria-label={isInterrupting ? "Stopping" : interruptActionLabel}
      title={parkedBackgroundWork ? "Stop background work owned by this provider" : "Interrupt the active turn"}
      onClick={() => void handleInterrupt()}
      disabled={isInterrupting}
      data-testid="session-chat-interrupt"
    >
      <svg
        className="session-chat-action-icon"
        width="16"
        height="16"
        viewBox="0 0 18 18"
        aria-hidden="true"
      >
        <rect
          x="5"
          y="5"
          width="8"
          height="8"
          rx="1"
          fill="currentColor"
        />
      </svg>
      <span className="session-chat-action-label">
        {isInterrupting ? "Stopping" : interruptActionLabel}
      </span>
    </Button>
  ) : null;
  const composerActions = (
    <>
      {isManagedLocal && sentConfirmation && !outboxInTranscript ? (
        <span className="session-chat-sent-notice">Sent</span>
      ) : null}
      {!(isMobileComposer && composerHeadVisible) ? stopInterruptButton : null}
      {canSteerNow && canQueueNow ? (
        <Button
          type="button"
          variant="secondary"
          size="sm"
          className="session-chat-btn session-chat-btn--queue"
          onClick={() => void handleSecondaryQueue()}
          disabled={
            isComposerDisabled ||
            (!draft.trim() && composerAttachments.attachments.length === 0) ||
            isSubmitting
          }
          aria-label="Queue"
          title="Queue for after this turn (Tab)"
        >
          <span className="session-chat-action-label">Queue</span>
          <kbd className="session-chat-key-hint" aria-hidden="true">
            Tab
          </kbd>
          <svg
            className="session-chat-action-icon"
            width="18"
            height="18"
            viewBox="0 0 18 18"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.4"
            aria-hidden="true"
          >
            <circle cx="9" cy="9" r="6" />
            <path d="M9 5v4l3 2" />
          </svg>
        </Button>
      ) : null}
      <Button
        type="submit"
        variant="primary"
        className="session-chat-btn session-chat-btn--send"
        aria-label={submitButtonLabel}
        size="sm"
        disabled={
          isComposerDisabled ||
          !hasComposerContent ||
          isSubmitting ||
          isSendBlocked ||
          composerAttachments.isCompressing
        }
        title={
          composerDisabledReason ||
          (isSendLocked ? turnNoticeText : undefined)
        }
      >
        <span className="session-chat-action-label">
          {submitButtonLabel}
        </span>
        <svg
          className="session-chat-action-icon"
          width="18"
          height="18"
          viewBox="0 0 18 18"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          aria-hidden="true"
        >
          <path d="M9 14V4M4.5 8.5 9 4l4.5 4.5" />
        </svg>
      </Button>
    </>
  );

  return (
    <div
      className="session-chat session-chat--dock"
      data-testid="session-continuation-panel"
    >

      {error && !errorShownInTranscript && (
        <div className="session-chat-error">
          <span>{error}</span>
          <button type="button" onClick={() => setError(null)}>
            Dismiss
          </button>
        </div>
      )}

      {isManagedLocal && isStalled ? (
        <div
          className="session-chat-stall-recovery"
          data-testid="session-chat-stall-recovery"
        >
          <div className="session-chat-stall-recovery__copy">
            <span className="session-chat-stall-recovery__title">
              Managed session appears stalled
            </span>
            <span className="session-chat-stall-recovery__detail">
              No progress has arrived from this managed session. Interrupt
              releases the current turn.
            </span>
          </div>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => void handleInterrupt()}
            disabled={isInterrupting}
          >
            {isInterrupting ? "Interrupting" : "Interrupt"}
          </Button>
        </div>
      ) : null}

      {turnEndedDraft && !outboxInTranscript ? (
        <div
          className="session-chat-queued session-chat-queued--failed"
          data-testid="session-chat-turn-ended"
        >
          <div className="session-chat-queued__label">Active turn ended</div>
          <div className="session-chat-queued__item">
            <span className="session-chat-queued__text">{turnEndedDraft.text}</span>
            <button
              type="button"
              className="session-chat-queued__cancel"
              onClick={() => void handleQueueInsteadAfterTurnEnded()}
            >
              Queue instead
            </button>
            <button
              type="button"
              className="session-chat-queued__cancel"
              onClick={() => setTurnEndedDraft(null)}
            >
              Dismiss
            </button>
          </div>
        </div>
      ) : null}

      {ownsManagedInputs && !outboxInTranscript && failedInputs.length > 0 ? (
        <div
          className="session-chat-queued session-chat-queued--failed"
          data-testid="session-chat-queued-failed"
        >
          <div className="session-chat-queued__label">Delivery failed</div>
          <ul className="session-chat-queued__list">
            {failedInputs.map((row) => {
              const clientRequestId = row.client_request_id;
              return (
                <li
                  key={
                    clientRequestId ??
                    row.live_input_id ??
                    row.id ??
                    row.text
                  }
                  className="session-chat-queued__item"
                >
                  <span className="session-chat-queued__text">{row.text}</span>
                  <span className="session-chat-queued__status session-chat-queued__status--failed">
                    {row.last_error || "failed"}
                  </span>
                  {clientRequestId ? (
                    <button
                      type="button"
                      className="session-chat-queued__cancel"
                      aria-label="Dismiss failed input"
                      onClick={() => handleDiscardPending(clientRequestId)}
                    >
                      Dismiss
                    </button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {isComposerDisabled && retainDockComposer ? (
        <p className="session-chat-control-note" role="status">
          {composerDisabledReason}
        </p>
      ) : null}

      {hostUpdateQueued ? (
        <div
          className="session-chat-host-link-queued"
          data-testid="session-chat-update-queued"
          role="status"
        >
          {HOST_LINK_COPY.sendQueued}
        </div>
      ) : null}
      <form
        className={`session-chat-composer session-chat-composer--dock${composerState.tone === "live" ? " session-chat-composer--running" : ""}`}
        onSubmit={handleSend}
        title={composerDisabledReason ?? undefined}
        onPaste={attachmentInputEnabled ? handleComposerPaste : undefined}
        onDrop={attachmentInputEnabled ? handleComposerDrop : undefined}
        onDragOver={attachmentInputEnabled ? handleComposerDragOver : undefined}
      >
        <span ref={leafRef} className="session-chat-composer__leaf" aria-hidden="true" />
        {composerHeadVisible ? (
          <div
            className="session-chat-composer__head"
            data-testid="session-chat-composer-head"
          >
            {/* A status line only while something is happening; idle lives in
                the input's placeholder. While the composer is unavailable the
                runtime-evidence strip still needs a home, so it rides here. */}
            {showComposerUnavailableState ? null : composerState.tone === "live" ? (
              <>
                <StatusBulb state="working" />
                <span className="session-chat-composer__head-label">
                  {composerWorkingLabel}
                </span>
                {composerHeadDetail ? (
                  <span
                    className="session-chat-composer__head-detail"
                    title={composerHeadDetail}
                    data-testid={
                      composerForegroundDetail
                        ? "session-chat-composer-foreground-detail"
                        : "session-chat-composer-delegated-detail"
                    }
                  >
                    {composerHeadDetail}
                  </span>
                ) : null}
                {composerElapsedSeconds != null ? (
                  <Nixie
                    value={formatElapsedClock(composerElapsedSeconds)}
                    flickerOnChange={false}
                  />
                ) : null}
              </>
            ) : composerState.tone === "attention" ? (
              <>
                <StatusBulb state="waiting" />
                <span className="session-chat-composer__head-label">
                  {composerState.text}
                </span>
              </>
            ) : composerState.tone === "unknown" ? (
              <>
                <StatusBulb state="unknown" />
                <span className="session-chat-composer__head-label">
                  {composerState.text}
                </span>
                {composerObservedClock ? (
                  <span className="session-chat-composer__head-detail">
                    last observed at {composerObservedClock}
                  </span>
                ) : null}
              </>
            ) : null}
            {isMobileComposer && stopInterruptButton ? (
              <span className="session-chat-composer__head-stop">
                {stopInterruptButton}
              </span>
            ) : null}
            {composerHeaderAccessory ? (
              <span className="session-chat-composer__head-accessory">
                {composerHeaderAccessory}
              </span>
            ) : null}
          </div>
        ) : null}
        <BackgroundTaskRoster
          delegation={session.session_state?.delegation}
          pendingBackgroundAgentCount={session.pending_background_agent_count}
          nowMs={activityNowMs}
          editedFilesLabel={editedFilesLabel}
          editedFilesTitle={editedFilesTitle}
        />
        {queuedBanner}
        {ownsManagedInputs && !outboxInTranscript
          ? pendingManagedLocalInputs
              .filter((pendingInput) => pendingInput.phase !== "delivered")
              .map((pendingInput) => (
                <div
                  className="session-chat-pending-message"
                  key={pendingInput.clientRequestId}
                >
                  <span className="session-chat-pending-message__text">
                    {pendingInput.text}
                  </span>
                  <span className="session-chat-pending-message__status">
                    {pendingInput.phase === "unknown"
                      ? pendingInput.attachmentsLost
                        ? ATTACHMENTS_LOST_ERROR
                        : "Not confirmed — retry with the same request"
                      : pendingInput.phase === "failed"
                        ? pendingInput.detail || "Not delivered"
                        : pendingInput.phase === "queued"
                          ? "Queued — server has this request"
                          : pendingInput.phase === "active"
                            ? "Console turn active"
                            : "Delivering..."}
                  </span>
                  {pendingInput.legacyModelMissing ? (
                    <span className="session-chat-pending-message__warning">
                      {LEGACY_MODEL_WARNING}
                    </span>
                  ) : null}
                  {pendingInput.attachments.length > 0 ? (
                    <span className="session-chat-pending-message__attachments">
                      {pendingInput.attachments
                        .map((attachment) => attachment.filename)
                        .join(", ")}
                    </span>
                  ) : null}
                  {pendingInput.phase === "unknown" &&
                  !pendingInput.attachmentsLost ? (
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      disabled={isSubmitting}
                      onClick={() =>
                        void handleManagedLocalSend(
                          pendingInput.text,
                          pendingInput.intent,
                          pendingInput.attachments,
                          {
                            existingClientRequestId:
                              pendingInput.clientRequestId,
                            model: pendingInput.model,
                            legacyModelMissing:
                              pendingInput.legacyModelMissing,
                          },
                        )
                      }
                    >
                      Retry
                    </Button>
                  ) : null}
                  {(pendingInput.phase === "unknown" ||
                    pendingInput.phase === "failed") &&
                  (!isManagedLocal || isComposerDisabled) &&
                  pendingInput.text.trim().length > 0 ? (
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      onClick={() => void copyToClipboard(pendingInput.text)}
                    >
                      Copy
                    </Button>
                  ) : null}
                  {pendingInput.phase === "failed" ? (
                    <>
                      {isManagedLocal && !isComposerDisabled ? (
                        <Button
                          type="button"
                          variant="secondary"
                          size="sm"
                          onClick={() => handleEditPending(pendingInput)}
                        >
                          Edit
                        </Button>
                      ) : null}
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        onClick={() =>
                          handleDiscardPending(pendingInput.clientRequestId)
                        }
                      >
                        Discard
                      </Button>
                    </>
                  ) : null}
                </div>
              ))
          : null}
        {showComposerUnavailableState ? (
          managedLaunchSuggestion ? (
            <ManagedLaunchHintCard
              suggestion={managedLaunchSuggestion}
              testId="session-chat-managed-launch-hint"
            />
          ) : (
            <div
              className="session-chat-composer-unavailable"
              data-testid="session-chat-disabled-reason"
            >
              <span className="session-chat-composer-unavailable__title">
                {composerDisabledTitle || "Control unavailable"}
              </span>
              <span className="session-chat-composer-unavailable__copy">
                {composerDisabledReason}
              </span>
              {composerDisabledAction ? (
                <div className="session-chat-composer-unavailable__action">
                  {composerDisabledAction}
                </div>
              ) : null}
            </div>
          )
        ) : (
          <>
            {blockedKeyboardSubmit ? (
              <div
                className="session-chat-confirmation"
                data-testid="session-chat-explicit-submit-hint"
              >
                {keyboardHintText || `Click "${submitLabel}" to confirm.`}
              </div>
            ) : null}
            {attachImagesEnabled &&
            (composerAttachments.attachments.length > 0 ||
              composerAttachments.error) ? (
              <AttachmentTray
                showAdd={false}
                attachments={composerAttachments.attachments}
                onAddFiles={composerAttachments.addFiles}
                onRemove={composerAttachments.removeAttachment}
                isCompressing={composerAttachments.isCompressing}
                error={composerAttachments.error}
                onClearError={composerAttachments.clearError}
                disabled={isSubmitting}
                addDisabled={!attachmentInputEnabled}
              />
            ) : null}
              <div
                className={`session-chat-composer-row${
                  hasComposerToolbar ? " session-chat-composer-row--stacked" : ""
                }`}
              >
                {hasComposerToolbar ? null : composerAttachButton}
                <div className="session-chat-composer-input">
                  <div className="session-chat-composer-line">
                    <textarea
                      ref={composerTextareaRef}
                      value={draft}
                      onChange={(e) => {
                        handleDraftChange(e.target.value);
                        autoResizeDockTextarea(e.target);
                      }}
                      onKeyDown={handleKeyDown}
                      placeholder={dockPlaceholder}
                      aria-label="Next instruction"
                      disabled={isSubmitting}
                      rows={1}
                    />
                  </div>
                </div>
                {hasComposerToolbar ? (
                  <div
                    className="session-chat-composer-toolbar"
                    data-testid="session-chat-composer-toolbar"
                  >
                    {composerAttachButton}
                    <ModelPicker
                      deviceId={session.device_id}
                      provider={session.provider}
                      value={selectedModel}
                      onChange={handleSelectedModelChange}
                      compact
                      testId="session-model-select"
                    />
                    <span className="session-chat-composer-toolbar__spacer" />
                    {!composerHeadVisible && composerHeaderAccessory ? (
                      <span className="session-chat-composer-row__accessory">
                        {composerHeaderAccessory}
                      </span>
                    ) : null}
                    {composerActions}
                  </div>
                ) : (
                  <>
                    {!composerHeadVisible && composerHeaderAccessory ? (
                      <span className="session-chat-composer-row__accessory">
                        {composerHeaderAccessory}
                      </span>
                    ) : null}
                    {composerActions}
                  </>
                )}
              </div>
          </>
        )}
      </form>
    </div>
  );
}
