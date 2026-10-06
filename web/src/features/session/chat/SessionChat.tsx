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
import { refreshAgentSessionProjectionTail } from "@/shared/api/useAgentSessions";
import type {
  ManagedLaunchSuggestion,
  TimelineItem,
} from "@/shared/session/model";
import { useComposerAttachments } from "./useComposerAttachments";
import { Badge, Button } from "@/shared/ui";
import { AttachmentTray } from "./AttachmentTray";
import { ManagedLaunchHintCard } from "../ManagedLaunchHintCard";
import type { OutboxEntry } from "../OutboxRow";
import { Nixie } from "@/shared/instruments/Nixie";
import { StatusBulb } from "@/shared/instruments/StatusLamp";
import { getRunningTurnStartMs } from "@/shared/instruments/toolActivity";
import {
  formatClockTime,
  formatElapsedClock,
  getSessionHeaderState,
} from "../sessionHeaderState";
import { ProviderGlyph } from "@/shared/ui/ProviderGlyph";
import { getProviderLabel } from "@/shared/lib/providers";
import ModelPicker from "@/features/launch/ModelPicker";
import { useWallClock } from "@/shared/hooks/useWallClock";
import {
  isActivityExecuting,
  isActivityStalled,
} from "@/shared/session/activityEvidence";
import { workingStatusLabel } from "@/shared/session/sessionStatus";
import "./session-chat.css";

interface PendingManagedLocalInput {
  text: string;
  clientRequestId: string;
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
const STALE_CONSOLE_TURN_DETAIL =
  "Console activity is stale; current delivery status is unconfirmed.";
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
  onClose?: () => void;
  emptyStateTitle?: string;
  hintText?: string;
  composerPlaceholder?: string;
  layout?: "panel" | "dock";
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
   * primary "Send update" action while the session is working; queue-next
   * becomes a secondary action. Turn-ended races surface as an inline
   * error with a "Queue instead" affordance.
   */
  canSteerActiveTurn?: boolean;
  /**
   * Durable timeline rows visible in the parent workspace. When present,
   * managed-local optimistic inputs stay visible until the backend-authored
   * user row with matching input identity arrives.
   */
  timelineItems?: TimelineItem[];
  /**
   * Rendered at the right of the composer's own head row (dock layout
   * only) — the runtime strip's evidence-disclosure icon lives here now,
   * not as a separate heading above the composer.
   */
  composerHeaderAccessory?: ReactNode;
  /**
   * When provided, in-flight, queued, and failed sends are reported here for
   * the transcript to render at its tail instead of stacking inside the
   * composer.
   */
  onOutboxChange?: (entries: OutboxEntry[]) => void;
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
    window.localStorage.setItem(
      key,
      JSON.stringify({ ...stored, deliveryConfirmed: true }),
    );
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
  onClose,
  emptyStateTitle,
  hintText,
  composerPlaceholder,
  layout = "panel",
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
  composerHeaderAccessory,
  onOutboxChange,
}: SessionChatProps) {
  const outboxInTranscript = Boolean(onOutboxChange);
  const activity = session.session_state.activity;
  const renderNowMs = Date.now();
  const activityNowMs = Math.max(
    renderNowMs,
    useWallClock(renderNowMs <= Date.parse(activity.valid_until ?? ""), 1_000),
  );
  const isSessionExecuting = isActivityExecuting(activity, activityNowMs);
  const isStalled = isActivityStalled(activity, activityNowMs);
  const isDock = layout === "dock";
  const isManagedLocal = chatMode === "managed_local";
  const isComposerDisabled = Boolean(composerDisabledReason);
  const attachImagesEnabled =
    isManagedLocal && Boolean(session.capabilities?.attach_images);
  const composerAttachments = useComposerAttachments();
  const hasHadComposer = useRef(false);
  if (!isComposerDisabled) hasHadComposer.current = true;
  const retainDockComposer =
    isDock && hasHadComposer.current && !composerDisabledAction;
  const showComposerUnavailableState =
    isComposerDisabled && !retainDockComposer;
  const queryClient = useQueryClient();
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
        setPendingManagedLocalInputs(
          stored.map(({ metadata, attachments, attachmentsLost }) => ({
            text: metadata.text,
            clientRequestId: metadata.clientRequestId,
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
            phase: metadata.deliveryConfirmed ? "delivered" : "unknown",
            attachmentsLost,
            detail: attachmentsLost ? ATTACHMENTS_LOST_ERROR : undefined,
          })),
        );
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
    refetchInterval: (query) => (query.state.data?.locked ? 2_000 : false),
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
    enabled: Boolean(session.id) && isManagedLocal,
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => {
      const rows = query.state.data ?? [];
      return rows.some((row) => {
        if (inTranscript(row)) return false;
        const turnState = row.turn?.state;
        if (turnState && NONTERMINAL_CONSOLE_TURN_STATES.has(turnState)) {
          return row.turn?.is_fresh === true;
        }
        return row.status === "queued" || row.status === "delivering";
      })
        ? 2_000
        : false;
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
        isManagedLocal &&
        pending.phase !== "failed",
      refetchOnWindowFocus: false,
      staleTime: 1_000,
      refetchInterval: (query: { state: { data: unknown } }) => {
        if (pending.phase === "delivered") return false;
        const row = query.state.data as QueuedInputSummary | null | undefined;
        const turnState = row?.turn?.state;
        if (turnState && NONTERMINAL_CONSOLE_TURN_STATES.has(turnState)) {
          return row?.turn?.is_fresh === true ? 2_000 : false;
        }
        return 2_000;
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
  const exactInputRows = useMemo(() => {
    const rows = new Map<string, QueuedInputSummary>();
    exactInputQueries.forEach((query, index) => {
      const row = query.data;
      const clientRequestId = pendingManagedLocalInputs[index]?.clientRequestId;
      if (row && clientRequestId) rows.set(clientRequestId, row);
    });
    return rows;
  }, [exactInputRowsKey, pendingManagedLocalInputs]);

  useEffect(() => {
    const linkedIds = pendingManagedLocalInputs
      .filter((pending) => ACCEPTED_INPUT_PHASES.has(pending.phase))
      .filter((pending) => {
        const receipt =
          exactInputRows.get(pending.clientRequestId) ??
          queuedInputsQuery.data?.find(
            (row) => row.client_request_id === pending.clientRequestId,
          );
        return inTranscript(receipt);
      })
      .map((pending) => pending.clientRequestId);
    if (linkedIds.length === 0) return;
    for (const clientRequestId of linkedIds) {
      clearInputOutbox(session.id, clientRequestId);
    }
    setPendingManagedLocalInputs((current) =>
      current.filter((pending) => !linkedIds.includes(pending.clientRequestId)),
    );
  }, [
    exactInputRows,
    pendingManagedLocalInputs,
    queuedInputsQuery.data,
    session.id,
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
      try {
        await persistInputOutbox(session.id, {
          text: message,
          intent,
          clientRequestId,
          model,
          attachments,
        });
      } catch (storageError) {
        const errorMessage =
          storageError instanceof Error
            ? storageError.message
            : "Could not persist input intent before sending";
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
      const nextPending: PendingManagedLocalInput = {
        text: message,
        clientRequestId,
        serverInputId: null,
        serverLiveInputId: null,
        intent,
        model,
        legacyModelMissing: options.legacyModelMissing,
        attachments,
        phase: "submitting",
        detail: options.autoRetryAttempt ? RECONNECTING_DETAIL : null,
      };
      setPendingManagedLocalInputs((current) => {
        const existing = current.some(
          (pending) => pending.clientRequestId === clientRequestId,
        );
        return existing
          ? current.map((pending) =>
              pending.clientRequestId === clientRequestId ? nextPending : pending,
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
        const result: SessionInputResponse = attachments.length
          ? await postSessionInputMultipart(session.id, {
              text: message,
              attachments,
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
          if (accepted.kind === "accepted") void refreshCurrentSessionWorkspace();
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
          void refreshCurrentSessionWorkspace();
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
          void refreshCurrentSessionWorkspace();
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
          void refreshCurrentSessionWorkspace();
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
        setIsSubmitting(false);
      }
    },
    [
      markInputDelivered,
      queryClient,
      refreshCurrentSessionWorkspace,
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
  const attachmentInputEnabled = attachImagesEnabled && !isSendLocked;
  // Inline interrupt is only offered while a turn is actually running — an
  // idle session has nothing to stop. When the stall-recovery card is showing
  // it already exposes the same action, so we hide the composer copy to avoid
  // two buttons doing the identical thing.
  const showInlineInterrupt = canInterruptTurn && isSendLocked && !isStalled;
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
  const attachmentSendBlocked =
    composerAttachments.attachments.length > 0 && primaryIntent !== "auto";
  // Attachment-only sends are valid when the route accepts them.
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
      if (hasAttachments && primaryIntent !== "auto") {
        setError(
          "Image attachments can only be sent when the session is ready for a new turn.",
        );
        return;
      }
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
    if (composerAttachments.attachments.length > 0) {
      setError("Image attachments can only be sent when the session is ready for a new turn.");
      return;
    }
    const message = draft.trim();
    if (!message) return;
    setDraft("");
    setError(null);
    const result = await handleManagedLocalSend(message, "queue", [], {
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
      setEditingPendingId(pending.clientRequestId);
      setDraft(pending.text);
      if (!pending.legacyModelMissing) {
        handleSelectedModelChange(pending.model ?? "");
      }
      composerAttachments.restore(pending.attachments);
      setError(null);
    },
    [composerAttachments, handleSelectedModelChange],
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
  const outboxEntries = useMemo<OutboxEntry[]>(() => {
    if (!isManagedLocal) return [];
    const rows = queuedInputsQuery.data ?? [];
    const receiptFor = (clientRequestId: string) =>
      exactInputRows.get(clientRequestId) ??
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

    const dismissedReceipts = readDismissedInputIds(session.id);
    for (const row of rows) {
      const clientRequestId = row.client_request_id;
      if (
        inTranscript(row) ||
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
      if (hasUnknownDeliveryError(row.last_error)) {
        inFlight.push({
          key,
          text: row.text,
          attachments,
          state: "unconfirmed",
          detail: hasRuntimeDrainingError(row.last_error)
            ? "runtime restarting"
            : row.last_error,
        });
      } else if (
        turnState &&
        NONTERMINAL_CONSOLE_TURN_STATES.has(turnState) &&
        row.turn?.is_fresh !== true
      ) {
        inFlight.push({
          key,
          text: row.text,
          attachments,
          state: "unconfirmed",
          detail: STALE_CONSOLE_TURN_DETAIL,
        });
      } else if (turnState === "queued") {
        queued.push({
          key,
          text: row.text,
          attachments,
          state: "queued",
          actions: [cancelAction(row)],
        });
      } else if (
        turnState &&
        NONTERMINAL_CONSOLE_TURN_STATES.has(turnState)
      ) {
        inFlight.push({ key, text: row.text, attachments, state: "sent" });
      } else if (row.status === "delivering") {
        inFlight.push({ key, text: row.text, attachments, state: "sending" });
      } else if (row.status === "queued") {
        queued.push({
          key,
          text: row.text,
          attachments,
          state: "queued",
          actions: [cancelAction(row)],
        });
      } else if (stoppedAfterDelivery(row)) {
        // Delivered, then stopped: the transcript already shows it.
      } else if (
        row.status === "failed" ||
        (row.status === "cancelled" && row.last_error)
      ) {
        failed.push({
          key,
          text: row.text,
          attachments,
          state: "failed",
          detail: row.last_error || "Input was cancelled before completion.",
          actions: clientRequestId
            ? [
                {
                  label: "Dismiss",
                  onClick: () =>
                    outboxActionsRef.current.discard(clientRequestId),
                },
              ]
            : undefined,
        });
      }
    }

    for (const pending of pendingManagedLocalInputs) {
      const key = `pending:${pending.clientRequestId}`;
      const receipt = receiptFor(pending.clientRequestId);
      const base = {
        key,
        text: pending.text,
        attachments:
          pending.attachmentSummaries ??
          summarizeInputAttachments(pending.attachments),
        warning: pending.legacyModelMissing ? LEGACY_MODEL_WARNING : null,
      };
      if (pending.phase === "delivered") {
        inFlight.push({ ...base, state: "sent" });
      } else if (pending.phase === "unknown") {
        inFlight.push({
          ...base,
          state: "unconfirmed",
          detail: pending.detail || UNCONFIRMED_DELIVERY_ERROR,
          actions: pending.attachmentsLost ? [] : [
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
          ],
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
            {
              label: "Edit",
              onClick: () => outboxActionsRef.current.edit(pending),
            },
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
          detail: pending.detail ?? null,
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
    return [...failed, ...inFlight, ...queued];
  }, [
    exactInputRows,
    isManagedLocal,
    isSubmitting,
    pendingManagedLocalInputs,
    queuedInputsQuery.data,
    turnEndedDraft,
  ]);
  useEffect(() => {
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

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      // Don't silently queue via Enter while working — require an explicit
      // click when the outcome would be "queued" so the user sees it.
      if (isSendLocked) {
        return;
      }
      if (requireClickForFirstSend && draft.trim() && !blockedKeyboardSubmit) {
        setBlockedKeyboardSubmit(true);
        return;
      }
      handleSend(e as unknown as FormEvent);
    }
  };

  const statusBadge = isComposerDisabled
    ? { variant: "warning" as const, label: "Unavailable" }
    : isSubmitting
      ? { variant: "warning" as const, label: "Sending" }
      : isStalled
        ? { variant: "warning" as const, label: "Stalled" }
        : isSendLocked
          ? { variant: "warning" as const, label: "Working" }
          : { variant: "neutral" as const, label: "Input available" };
  const submitButtonLabel = !isSendLocked
    ? submitLabel
    : canSteerNow
      ? "Send update"
      : canQueueNow
        ? "Queue next"
        : queueFull
          ? "Queue full"
          : "Waiting";
  const turnNoticeText = canSteerNow
    ? "Send update reaches the active turn. Queue next waits for its boundary. Enter does not send while a turn is active."
    : "Queue next waits for the next turn boundary. Enter does not queue while a turn is active.";

  // Composer header: ember + the server's working label + a mono timer while
  // a turn is active, ember + the server's attention copy when a provider question is
  // pending, or a cool dot + "Idle" + when the last turn ended. Shares its
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
  const composerElapsedSeconds =
    composerState.tone === "live" && turnStartMs != null
      ? Math.max(0, Math.floor((activityNowMs - turnStartMs) / 1_000))
      : null;
  // The server's label, verbatim, exactly as the session header shows it. It
  // only renders while the header's freshness gate calls the session live.
  const composerWorkingLabel = workingStatusLabel(session.session_state);
  const composerLastTurnMs = Date.parse(
    session.session_state.last_result_at ?? "",
  );
  const composerIdleClock = formatClockTime(composerLastTurnMs);
  const composerObservedClock = formatClockTime(Date.parse(activity.observed_at ?? ""));
  // Dock: the status line shows only while something runs or needs a
  // decision; at rest the placeholder carries it ("Idle since 2:21 AM —
  // message to continue").
  const composerHeadVisible =
    showComposerUnavailableState || composerState.tone !== "cool";
  const dockPlaceholder =
    composerState.tone === "cool"
      ? `${composerState.text} — message to continue`
      : composerPlaceholder || "Message";

  // Dock layout: this renders inside the composer frame itself, between
  // the head row and the input (see below) — one framed object, not a
  // separate block floating above it. Non-dock (panel) layout keeps it
  // above the composer, where it has always lived.
  const queuedBanner =
    isManagedLocal && !outboxInTranscript && activeQueuedInputs.length > 0 ? (
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

  return (
    <div
      className={`session-chat${isDock ? " session-chat--dock" : ""}`}
      data-testid={isDock ? "session-continuation-panel" : undefined}
    >
      {isDock ? null : (
        <div className="session-chat-header">
          <div className="session-chat-info">
            {onClose && (
              <button
                type="button"
                className="session-chat-back"
                onClick={onClose}
                title="Back to details"
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                >
                  <path d="M19 12H5M12 19l-7-7 7-7" />
                </svg>
              </button>
            )}
            <div className="session-chat-titles">
              {session.project ? (
                <span className="session-chat-title">{session.project}</span>
              ) : null}
              <span className="session-chat-provider">
                <ProviderGlyph provider={session.provider} size={16} />
                {getProviderLabel(session.provider)}
              </span>
            </div>
          </div>
          <div className="session-chat-status">
            <Badge variant={statusBadge.variant}>{statusBadge.label}</Badge>
          </div>
        </div>
      )}

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

      {isSendLocked && !isStalled && !isDock && (
        <div className="session-chat-turn-notice">
          <span>{turnNoticeText}</span>
        </div>
      )}

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

      {!isDock ? queuedBanner : null}

      {isManagedLocal && !outboxInTranscript && failedInputs.length > 0 ? (
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

      {isDock ? null : (
        <div className="session-chat-messages">
          <div className="session-chat-empty">
            <p>
              {emptyStateTitle || "Start a conversation with this session."}
            </p>
            <p className="session-chat-hint">
              {hintText ||
                (isManagedLocal
                  ? `Longhouse will send your next prompt into the live ${session.provider} session.`
                  : "Earlier synced turns stay visible here. Your first message continues from that context.")}
            </p>
          </div>
        </div>
      )}
      {isComposerDisabled && retainDockComposer ? (
        <p className="session-chat-control-note" role="status">
          {composerDisabledReason}
        </p>
      ) : null}

      <form
        className={`session-chat-composer${isDock ? " session-chat-composer--dock" : ""}${isDock && composerState.tone === "live" ? " session-chat-composer--running" : ""}`}
        onSubmit={handleSend}
        title={composerDisabledReason ?? undefined}
        onPaste={attachmentInputEnabled ? handleComposerPaste : undefined}
        onDrop={attachmentInputEnabled ? handleComposerDrop : undefined}
        onDragOver={attachmentInputEnabled ? handleComposerDragOver : undefined}
      >
        {isDock ? (
          <>
            <span className="session-chat-composer__leaf" aria-hidden="true" />
            <span
              className="session-chat-composer__point session-chat-composer__point--tl"
              aria-hidden="true"
            />
            <span
              className="session-chat-composer__point session-chat-composer__point--tr"
              aria-hidden="true"
            />
            <span
              className="session-chat-composer__point session-chat-composer__point--bl"
              aria-hidden="true"
            />
            <span
              className="session-chat-composer__point session-chat-composer__point--br"
              aria-hidden="true"
            />
          </>
        ) : null}
        {isDock && composerHeadVisible ? (
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
            {showComposerUnavailableState && composerHeaderAccessory ? (
              <span className="session-chat-composer__head-accessory">
                {composerHeaderAccessory}
              </span>
            ) : null}
          </div>
        ) : null}
        {isDock ? queuedBanner : null}
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
            ) : isDock ? null : (
              <div
                className="session-chat-confirmation session-chat-confirmation--spacer"
                aria-hidden="true"
              />
            )}
            {isManagedLocal && !outboxInTranscript
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
                      {pendingInput.phase === "failed" ? (
                        <>
                          <Button
                            type="button"
                            variant="secondary"
                            size="sm"
                            onClick={() => handleEditPending(pendingInput)}
                          >
                            Edit
                          </Button>
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
            {attachImagesEnabled &&
            (!isDock ||
              composerAttachments.attachments.length > 0 ||
              composerAttachments.error) ? (
              <AttachmentTray
                showAdd={!isDock}
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
            {isDock ? (
              <div className="session-chat-composer-row">
                {attachImagesEnabled ? (
                  <AttachmentTray
                    showThumbs={false}
                    attachments={composerAttachments.attachments}
                    onAddFiles={composerAttachments.addFiles}
                    onRemove={composerAttachments.removeAttachment}
                    isCompressing={composerAttachments.isCompressing}
                    disabled={isSubmitting}
                    addDisabled={!attachmentInputEnabled}
                  />
                ) : null}
                <div className="session-chat-composer-input">
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
                  {session.session_state.mode === "console" || composerHeaderAccessory ? (
                    <div
                      className="session-chat-composer-chips"
                      data-testid="session-chat-composer-chips"
                    >
                      {session.session_state.mode === "console" ? (
                        <ModelPicker
                          deviceId={session.device_id}
                          provider={session.provider}
                          value={selectedModel}
                          onChange={handleSelectedModelChange}
                          compact
                          testId="session-model-select"
                        />
                      ) : null}
                      {composerHeaderAccessory ? (
                        <span className="session-chat-composer__head-accessory">
                          {composerHeaderAccessory}
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                </div>
                {isManagedLocal && sentConfirmation && !outboxInTranscript ? (
                  <span className="session-chat-sent-notice">Sent</span>
                ) : null}
                {showInlineInterrupt ? (
                  <Button
                    type="button"
                    variant="danger"
                    size="sm"
                    className="session-chat-btn session-chat-btn--stop"
                    aria-label={isInterrupting ? "Stopping" : "Stop"}
                    title="Interrupt the active turn"
                    onClick={() => void handleInterrupt()}
                    disabled={isInterrupting}
                    data-testid="session-chat-interrupt"
                  >
                    <span className="session-chat-action-label">
                      {isInterrupting ? "Stopping" : "Stop"}
                    </span>
                    <svg
                      className="session-chat-action-icon"
                      width="18"
                      height="18"
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
                  </Button>
                ) : null}
                {canSteerNow && canQueueNow ? (
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    className="session-chat-btn session-chat-btn--queue"
                    onClick={() => void handleSecondaryQueue()}
                    disabled={
                      isComposerDisabled || !draft.trim() || isSubmitting
                    }
                    aria-label="Queue next"
                    title="Queue for the next turn boundary"
                  >
                    <span className="session-chat-action-label">
                      Queue next
                    </span>
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
                    attachmentSendBlocked ||
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
              </div>
            ) : (
              <>
                <textarea
                  ref={composerTextareaRef}
                  value={draft}
                  onChange={(e) => handleDraftChange(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={composerPlaceholder || "Message"}
                  disabled={isComposerDisabled || isSubmitting}
                  rows={2}
                  title={composerDisabledReason ?? undefined}
                />
                <div className="session-chat-actions">
                  {showInlineInterrupt ? (
                    <Button
                      type="button"
                      variant="danger"
                      size="sm"
                      onClick={() => void handleInterrupt()}
                      disabled={isInterrupting}
                      data-testid="session-chat-interrupt"
                    >
                      {isInterrupting ? "Stopping" : "Stop"}
                    </Button>
                  ) : null}
                  {canSteerNow && canQueueNow ? (
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      onClick={() => void handleSecondaryQueue()}
                      disabled={
                        isComposerDisabled || !draft.trim() || isSubmitting
                      }
                    >
                      Queue next
                    </Button>
                  ) : null}
                  <Button
                    type="submit"
                    variant="primary"
                    size="sm"
                    disabled={
                      isComposerDisabled ||
                      !hasComposerContent ||
                      isSubmitting ||
                      isSendBlocked ||
                      attachmentSendBlocked ||
                      composerAttachments.isCompressing
                    }
                    title={composerDisabledReason ?? undefined}
                  >
                    {submitButtonLabel}
                  </Button>
                </div>
              </>
            )}
          </>
        )}
      </form>
    </div>
  );
}
