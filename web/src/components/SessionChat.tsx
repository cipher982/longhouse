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
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  cancelSessionInput,
  fetchSessionInputs,
  fetchSessionLockStatus,
  interruptLiveSession,
  interruptConsoleTurn,
  postSessionInput,
  postSessionInputMultipart,
  type QueuedInputSummary,
  type SessionLockInfo,
} from "../services/api";
import type { AgentSession } from "../services/api/agents";
import type {
  ManagedLaunchSuggestion,
  TimelineItem,
} from "../lib/sessionWorkspace";
import { useComposerAttachments } from "../lib/useComposerAttachments";
import { Badge, Button } from "./ui";
import { AttachmentTray } from "./AttachmentTray";
import { ManagedLaunchHintCard } from "./session-workspace/ManagedLaunchHintCard";
import type { OutboxEntry } from "./session-workspace/OutboxRow";
import { Nixie } from "./instruments/Nixie";
import { getRunningTurnStartMs } from "./instruments/toolActivity";
import {
  formatClockTime,
  formatElapsedClock,
  getSessionHeaderState,
} from "./session-workspace/sessionHeaderState";
import { ProviderGlyph } from "./ProviderGlyph";
import { getProviderLabel } from "../lib/providers";
import { useWallClock } from "../hooks/useWallClock";
import {
  isActivityExecuting,
  isActivityStalled,
} from "../lib/activityEvidence";
import "../styles/session-chat.css";

interface PendingManagedLocalInput {
  text: string;
  clientRequestId: string;
  serverInputId: number | null;
  intent: "auto" | "queue" | "steer";
  attachments: { blob: Blob; filename: string }[];
  /** `delivered`: the server confirmed handoff; the row stays until the
   *  transcript echoes it so the message never blinks out in between. */
  phase: "submitting" | "queued" | "unknown" | "delivered";
  deliveredAt?: number;
  /** Same-text user rows that cannot be this send's echo: those loaded when
   *  it started, plus rows other sends have already claimed. */
  echoExcludedRowIds?: string[];
}

/** A delivered message normally echoes within seconds. If the echo never
 *  lands in the loaded transcript, stop showing the provisional row. */
const DELIVERED_ECHO_GRACE_MS = 60_000;
const UNCONFIRMED_DELIVERY_ERROR =
  "Delivery is not confirmed; retry with the same request.";

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
  "id" | "project" | "provider" | "capabilities" | "session_state"
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
  attachments: { filename: string; type: string; size: number }[];
  createdAt: number;
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
    attachments: pending.attachments.map(({ blob, filename }) => ({
      filename,
      type: blob.type || "application/octet-stream",
      size: blob.size,
    })),
    createdAt: Date.now(),
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
  }[]
> {
  const metadata = readInputOutboxes(sessionId);
  const loaded: {
    metadata: StoredInputOutbox;
    attachments: { blob: Blob; filename: string }[];
  }[] = [];
  for (const entry of metadata) {
    if (entry.attachments.length === 0) {
      loaded.push({ metadata: entry, attachments: [] });
      continue;
    }
    const payload = await readInputOutboxPayload(
      sessionId,
      entry.clientRequestId,
    );
    if (!payload) throw new Error("Stored attachment payload is missing");
    loaded.push({ metadata: entry, attachments: payload.attachments });
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
function normalizeInputText(value: string | null | undefined): string {
  return (value ?? "").replace(/\s+/g, " ").trim();
}

function userRowIdsWithText(
  timelineItems: TimelineItem[] | undefined,
  text: string,
): string[] {
  const target = normalizeInputText(text);
  if (!target || !timelineItems) return [];
  const ids: string[] = [];
  for (const item of timelineItems) {
    if (item.kind !== "message") continue;
    const { event } = item;
    if (event.role !== "user" || event.is_head_branch === false) continue;
    if (normalizeInputText(event.content_text) === target) {
      ids.push(String(event.id));
    }
  }
  return ids;
}

/** Delivery is already confirmed, so only the visible row is at stake: the
 *  receipt-to-event link lands after ingest, and a new same-text user row
 *  nobody else has claimed is this message seen before its identity is
 *  stamped. Returns that row's id. */
function deliveredTextEchoRowId(
  timelineItems: TimelineItem[],
  pendingInput: PendingManagedLocalInput,
  claimedRowIds: Set<string>,
): string | null {
  if (
    pendingInput.phase !== "delivered" ||
    pendingInput.echoExcludedRowIds == null
  ) {
    return null;
  }
  const excluded = new Set(pendingInput.echoExcludedRowIds);
  return (
    userRowIdsWithText(timelineItems, pendingInput.text).find(
      (id) => !excluded.has(id) && !claimedRowIds.has(id),
    ) ?? null
  );
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
  return code === "delivery_unknown" || code === "input_receipt_unknown";
}

function hasUnknownDeliveryError(error?: string | null): boolean {
  return (
    hasProviderDeliveryUnknownError(error) || hasRuntimeDrainingError(error)
  );
}

function structuredInputErrorCode(error: unknown): string | null {
  if (!error || typeof error !== "object" || !("body" in error)) return null;
  const body = error.body;
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const detail = body.detail;
  if (!detail || typeof detail !== "object") return null;
  const code = "error_code" in detail ? detail.error_code : "code" in detail ? detail.code : null;
  return typeof code === "string" ? code.trim().toLowerCase() : null;
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
  const retainDockComposer = isDock && hasHadComposer.current;
  const showComposerUnavailableState =
    isComposerDisabled && !retainDockComposer;
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isInterrupting, setIsInterrupting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blockedKeyboardSubmit, setBlockedKeyboardSubmit] = useState(false);

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
          stored.map(({ metadata, attachments }) => ({
            text: metadata.text,
            clientRequestId: metadata.clientRequestId,
            serverInputId: null,
            intent: metadata.intent,
            attachments,
            phase: "unknown",
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
  const timelineItemsRef = useRef(timelineItems);
  timelineItemsRef.current = timelineItems;
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
      queryClient.invalidateQueries({
        queryKey: ["agent-session-projection-infinite", session.id],
      }),
      queryClient.invalidateQueries({
        queryKey: ["agent-session-events", session.id],
      }),
      queryClient.invalidateQueries({
        queryKey: ["agent-session-events-infinite", session.id],
      }),
      queryClient.invalidateQueries({ queryKey: ["agent-sessions"] }),
    ]);
  }, [queryClient, session.id]);

  useEffect(() => {
    if (pendingManagedLocalInputs.length === 0 || !timelineItems) return;
    // Oldest first, and each echo row resolves exactly one send: a row
    // claimed here is excluded for every later same-text send, now and in
    // later passes, so one echo never clears two identical sends.
    const claimedRowIds = new Set<string>();
    const resolvedIds: string[] = [];
    for (const pending of pendingManagedLocalInputs) {
      const identityRow =
        pending.attachments.length === 0 || pending.phase === "delivered"
          ? durableSubmittedInputRowId(timelineItems, pending)
          : null;
      const echoRow =
        identityRow ??
        deliveredTextEchoRowId(timelineItems, pending, claimedRowIds);
      if (echoRow == null) continue;
      resolvedIds.push(pending.clientRequestId);
      claimedRowIds.add(echoRow);
    }
    if (resolvedIds.length === 0) return;
    for (const clientRequestId of resolvedIds) {
      clearInputOutbox(session.id, clientRequestId);
    }
    setPendingManagedLocalInputs((current) =>
      current
        .filter((pending) => !resolvedIds.includes(pending.clientRequestId))
        .map((pending) =>
          pending.echoExcludedRowIds == null
            ? pending
            : {
                ...pending,
                echoExcludedRowIds: [
                  ...new Set([
                    ...pending.echoExcludedRowIds,
                    ...claimedRowIds,
                  ]),
                ],
              },
        ),
    );
  }, [pendingManagedLocalInputs, session.id, timelineItems]);

  // Delivery is settled once the server confirms it, so the durable retry
  // slot is released immediately; only the visible row waits for the echo.
  const markInputDelivered = useCallback(
    (clientRequestId: string, serverInputId: number | null | undefined) => {
      clearInputOutbox(session.id, clientRequestId);
      const deliveredAt = Date.now();
      setPendingManagedLocalInputs((current) =>
        current.map((pending) =>
          pending.clientRequestId === clientRequestId &&
          pending.phase !== "delivered"
            ? {
                ...pending,
                phase: "delivered",
                deliveredAt,
                serverInputId: serverInputId ?? pending.serverInputId,
              }
            : pending,
        ),
      );
    },
    [session.id],
  );

  useEffect(() => {
    const delivered = pendingManagedLocalInputs.filter(
      (pending) => pending.phase === "delivered",
    );
    if (delivered.length === 0) return;
    const oldest = Math.min(
      ...delivered.map((pending) => pending.deliveredAt ?? Date.now()),
    );
    const timer = setTimeout(
      () => {
        const cutoff = Date.now() - DELIVERED_ECHO_GRACE_MS;
        setPendingManagedLocalInputs((current) =>
          current.filter(
            (pending) =>
              pending.phase !== "delivered" ||
              (pending.deliveredAt ?? 0) > cutoff,
          ),
        );
      },
      Math.max(0, oldest + DELIVERED_ECHO_GRACE_MS - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [pendingManagedLocalInputs]);

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
      return rows.some(
        (row) => row.status === "queued" || row.status === "delivering",
      )
        ? 2_000
        : false;
    },
    staleTime: 10_000,
  });
  useEffect(() => {
    if (pendingManagedLocalInputs.length === 0 || !queuedInputsQuery.data)
      return;
    const resolvedIds: string[] = [];
    const nextInputs = pendingManagedLocalInputs.map<PendingManagedLocalInput>(
      (pending) => {
        const receipt = queuedInputsQuery.data.find(
          (row) => row.client_request_id === pending.clientRequestId,
        );
        if (!receipt || pending.phase === "delivered") return pending;
        if (receipt.status === "delivered") {
          clearInputOutbox(session.id, pending.clientRequestId);
          return {
            ...pending,
            phase: "delivered",
            deliveredAt: Date.now(),
            serverInputId: receipt.id ?? pending.serverInputId,
          };
        }
        if (
          receipt.status === "cancelled" ||
          (receipt.status === "failed" &&
            !hasUnknownDeliveryError(receipt.last_error))
        ) {
          resolvedIds.push(pending.clientRequestId);
          setError(
            receipt.last_error ||
              (receipt.status === "cancelled"
                ? "Input was cancelled before delivery."
                : "Input delivery failed."),
          );
          return pending;
        }
        if (hasUnknownDeliveryError(receipt.last_error)) {
          return {
            ...pending,
            serverInputId: receipt.id ?? null,
            phase: "unknown",
          };
        }
        return {
          ...pending,
          serverInputId: receipt.id ?? null,
          phase: receipt.status === "queued" ? "queued" : "unknown",
        };
      },
    );
    if (resolvedIds.length > 0) {
      for (const clientRequestId of resolvedIds) {
        clearInputOutbox(session.id, clientRequestId);
      }
      setPendingManagedLocalInputs((current) =>
        current.filter(
          (pending) => !resolvedIds.includes(pending.clientRequestId),
        ),
      );
    } else if (
      nextInputs.some(
        (pending, index) =>
          pending.phase !== pendingManagedLocalInputs[index]?.phase ||
          pending.serverInputId !==
            pendingManagedLocalInputs[index]?.serverInputId,
      )
    ) {
      setPendingManagedLocalInputs(nextInputs);
    }
  }, [pendingManagedLocalInputs, queuedInputsQuery.data, session.id]);
  const activeQueuedInputs = (queuedInputsQuery.data ?? []).filter(
    (row) =>
      !(row.intent === "steer" && row.last_error === "turn_ended") &&
      (row.status === "queued" ||
        row.status === "delivering" ||
        hasUnknownDeliveryError(row.last_error)),
  );
  const queueFull =
    activeQueuedInputs.filter((row) => row.status === "queued").length >= 5;
  const failedInputs = (queuedInputsQuery.data ?? []).filter(
    (row) =>
      (row.status === "failed" || row.status === "cancelled") &&
      !hasUnknownDeliveryError(row.last_error),
  );
  // Offer an explicit "Queue instead" fallback instead of silently remapping
  // the user's original steer intent.
  const [turnEndedDraft, setTurnEndedDraft] = useState<string | null>(null);
  const handleManagedLocalSend = useCallback(
    async (
      message: string,
      intent: "auto" | "queue" | "steer" = "auto",
      attachments: { blob: Blob; filename: string }[] = [],
      existingClientRequestId?: string,
    ) => {
      const clientRequestId = existingClientRequestId ?? newClientRequestId();
      try {
        await persistInputOutbox(session.id, {
          text: message,
          intent,
          clientRequestId,
          attachments,
        });
      } catch (storageError) {
        setError(
          storageError instanceof Error
            ? storageError.message
            : "Could not persist input intent before sending",
        );
        return false;
      }
      const nextPending: PendingManagedLocalInput = {
        text: message,
        clientRequestId,
        serverInputId: null,
        intent,
        attachments,
        phase: "submitting",
        echoExcludedRowIds: userRowIdsWithText(
          timelineItemsRef.current,
          message,
        ),
      };
      setPendingManagedLocalInputs((current) => {
        const existing = current.some(
          (pending) => pending.clientRequestId === clientRequestId,
        );
        return existing
          ? current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? nextPending
                : pending,
            )
          : [...current, nextPending];
      });
      setIsSubmitting(true);
      try {
        const result = attachments.length
          ? await postSessionInputMultipart(session.id, {
              text: message,
              attachments,
              client_request_id: clientRequestId,
            })
          : await postSessionInput(session.id, {
              text: message,
              intent,
              client_request_id: clientRequestId,
            });

        queryClient.setQueryData<QueuedInputSummary[]>(
          ["session-inputs", session.id],
          result.queued,
        );
        const receipt = result.queued.find(
          (row) => row.client_request_id === clientRequestId,
        );
        const terminalStatus = receipt?.status;
        if (terminalStatus === "delivered") {
          markInputDelivered(clientRequestId, result.input_id ?? receipt?.id);
          return true;
        }
        if (
          terminalStatus === "cancelled" ||
          (terminalStatus === "failed" &&
            !hasUnknownDeliveryError(receipt?.last_error))
        ) {
          clearInputOutbox(session.id, clientRequestId);
          setPendingManagedLocalInputs((current) =>
            current.filter(
              (pending) => pending.clientRequestId !== clientRequestId,
            ),
          );
          setError(
            receipt?.last_error ||
              (terminalStatus === "cancelled"
                ? "Input was cancelled before delivery."
                : "Input delivery failed."),
          );
          return false;
        }
        const consoleTurnAccepted =
          result.turn &&
          ["queued", "starting", "active", "draining"].includes(
            result.turn.state,
          ) &&
          (result.outcome === "sent" || result.outcome === "queued");
        if (consoleTurnAccepted) {
          if (result.outcome === "queued") {
            // The Console turn is durable, but it is not delivered until the
            // provider claims it. Keep the outbox entry so an app restart can
            // replay the same client_request_id instead of silently losing an
            // image while the FIFO head is starting or ambiguous.
            setPendingManagedLocalInputs((current) =>
              current.map((pending) =>
                pending.clientRequestId === clientRequestId
                  ? {
                      ...pending,
                      phase: "queued",
                    }
                  : pending,
              ),
            );
          } else {
            markInputDelivered(clientRequestId, result.input_id);
          }
          void refreshCurrentSessionWorkspace();
          return true;
        }
        if (result.outcome === "unknown") {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...pending, phase: "unknown" }
                : pending,
            ),
          );
          setError(UNCONFIRMED_DELIVERY_ERROR);
          return false;
        }
        if (
          (result.client_request_id &&
            result.client_request_id !== clientRequestId) ||
          (!result.client_request_id && !receipt)
        ) {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...pending, phase: "unknown" }
                : pending,
            ),
          );
          setError(UNCONFIRMED_DELIVERY_ERROR);
          return false;
        }
        if (result.outcome === "sent") {
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
            2000,
          );
          void refreshCurrentSessionWorkspace();
          markInputDelivered(clientRequestId, result.input_id ?? receipt?.id);
        } else if (
          receipt?.status !== "cancelled" &&
          hasUnknownDeliveryError(receipt?.last_error)
        ) {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...pending, phase: "unknown" }
                : pending,
            ),
          );
          setError(UNCONFIRMED_DELIVERY_ERROR);
          return false;
        } else if (receipt?.status === "queued") {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? {
                    ...pending,
                    serverInputId: receipt.id ?? null,
                    phase: "queued",
                  }
                : pending,
            ),
          );
        } else if (
          receipt?.status === "failed" ||
          receipt?.status === "cancelled"
        ) {
          clearInputOutbox(session.id, clientRequestId);
          setPendingManagedLocalInputs((current) =>
            current.filter(
              (pending) => pending.clientRequestId !== clientRequestId,
            ),
          );
          setError(
            receipt.last_error ||
              (receipt.status === "cancelled"
                ? "Input was cancelled before delivery."
                : "Input delivery failed."),
          );
          return false;
        } else {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...pending, phase: "unknown" }
                : pending,
            ),
          );
          setError(UNCONFIRMED_DELIVERY_ERROR);
          return false;
        }
        return true;
      } catch (e) {
        let persistedReceipt: QueuedInputSummary | undefined;
        try {
          const refreshedInputs = await fetchSessionInputs(session.id);
          queryClient.setQueryData<QueuedInputSummary[]>(
            ["session-inputs", session.id],
            refreshedInputs,
          );
          persistedReceipt = refreshedInputs.find(
            (row) => row.client_request_id === clientRequestId,
          );
        } catch {
          // Preserve the request error when the receipt cannot be refreshed.
        }
        if (persistedReceipt?.status === "delivered") {
          markInputDelivered(clientRequestId, persistedReceipt.id);
          return true;
        }
        if (
          persistedReceipt?.status !== "cancelled" &&
          hasUnknownDeliveryError(persistedReceipt?.last_error)
        ) {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...pending, phase: "unknown" }
                : pending,
            ),
          );
          setError(UNCONFIRMED_DELIVERY_ERROR);
          return false;
        }
        if (
          persistedReceipt?.status === "failed" ||
          persistedReceipt?.status === "cancelled"
        ) {
          clearInputOutbox(session.id, clientRequestId);
          setPendingManagedLocalInputs((current) =>
            current.filter(
              (pending) => pending.clientRequestId !== clientRequestId,
            ),
          );
          setError(
            persistedReceipt.last_error ||
              (persistedReceipt.status === "cancelled"
                ? "Input was cancelled before delivery."
                : "Input delivery failed."),
          );
          return false;
        }
        if (persistedReceipt?.status === "queued") {
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? {
                    ...pending,
                    serverInputId: persistedReceipt.id ?? null,
                    phase: "queued",
                  }
                : pending,
            ),
          );
          setError(null);
          return true;
        }
        const errorBody = (
          e as {
            body?: {
              detail?: { message?: string };
            };
          }
        )?.body;
        const errorCode = structuredInputErrorCode(e);
        if (intent === "steer" && errorCode === "turn_ended") {
          setTurnEndedDraft(message);
          setError(
            errorBody?.detail?.message ??
              "Active turn ended before your update arrived.",
          );
          clearInputOutbox(session.id, clientRequestId);
          setPendingManagedLocalInputs((current) =>
            current.filter(
              (pending) => pending.clientRequestId !== clientRequestId,
            ),
          );
        } else {
          setError(
            e instanceof Error
              ? `${e.message}. ${UNCONFIRMED_DELIVERY_ERROR}`
              : UNCONFIRMED_DELIVERY_ERROR,
          );
          setPendingManagedLocalInputs((current) =>
            current.map((pending) =>
              pending.clientRequestId === clientRequestId
                ? { ...pending, phase: "unknown" }
                : pending,
            ),
          );
        }
        return false;
      } finally {
        setIsSubmitting(false);
      }
    },
    [
      markInputDelivered,
      queryClient,
      refreshCurrentSessionWorkspace,
      session.id,
    ],
  );

  const handleCancelQueuedInput = useCallback(
    async (input: QueuedInputSummary) => {
      try {
        await cancelSessionInput(session.id, input);
        // Optimistically drop it from the cache; refetch to confirm.
        queryClient.setQueryData<QueuedInputSummary[]>(
          ["session-inputs", session.id],
          (rows = []) =>
            rows.filter((row) =>
              input.live_input_id
                ? row.live_input_id !== input.live_input_id
                : row.id !== input.id,
            ),
        );
        const clientRequestId = input.client_request_id;
        if (clientRequestId) {
          clearInputOutbox(session.id, clientRequestId);
          setPendingManagedLocalInputs((current) =>
            current.filter(
              (pending) => pending.clientRequestId !== clientRequestId,
            ),
          );
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
      // Attachments require message text only when the user wrote nothing —
      // attachment-only sends are valid and the server accepts text="".
      if (!message && !hasAttachments) return;
      if (isSubmitting || isComposerDisabled || isSendBlocked) return;
      if (hasAttachments && primaryIntent !== "auto") {
        setError(
          "Image attachments can only be sent when the session is ready for a new turn.",
        );
        return;
      }
      // Block send while compression is in flight; the snapshot would miss
      // the pending file and the late add could repopulate the cleared tray.
      if (composerAttachments.isCompressing) return;

      setDraft("");
      setError(null);
      setBlockedKeyboardSubmit(false);

      const attachmentArgs = hasAttachments
        ? pendingAttachments.map((a) => ({
            blob: a.blob,
            filename: a.filename,
          }))
        : [];
      const sent = await handleManagedLocalSend(
        message,
        primaryIntent,
        attachmentArgs,
      );
      if (sent) {
        if (hasAttachments) composerAttachments.clear();
      } else {
        setDraft(message);
      }
    },
    [
      draft,
      isSubmitting,
      handleManagedLocalSend,
      isComposerDisabled,
      isSendBlocked,
      primaryIntent,
      composerAttachments,
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
    const message = draft.trim();
    if (!message || isSubmitting || isComposerDisabled || !canQueueNow) return;
    setDraft("");
    setError(null);
    const sent = await handleManagedLocalSend(message, "queue");
    if (!sent) setDraft(message);
  }, [
    draft,
    isSubmitting,
    isComposerDisabled,
    canQueueNow,
    handleManagedLocalSend,
  ]);

  const handleQueueInsteadAfterTurnEnded = useCallback(async () => {
    if (!turnEndedDraft) return;
    setError(null);
    const text = turnEndedDraft;
    setTurnEndedDraft(null);
    const sent = await handleManagedLocalSend(text, "queue");
    if (!sent) setTurnEndedDraft(text);
  }, [turnEndedDraft, handleManagedLocalSend]);

  // Actions go through a ref so the reported entries only change when what
  // the user sees changes, not whenever a handler closure is rebuilt.
  const outboxActionsRef = useRef({
    retry: handleManagedLocalSend,
    cancel: handleCancelQueuedInput,
    queueInstead: handleQueueInsteadAfterTurnEnded,
  });
  outboxActionsRef.current = {
    retry: handleManagedLocalSend,
    cancel: handleCancelQueuedInput,
    queueInstead: handleQueueInsteadAfterTurnEnded,
  };
  const outboxEntries = useMemo<OutboxEntry[]>(() => {
    if (!isManagedLocal) return [];
    const rows = queuedInputsQuery.data ?? [];
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

    for (const row of rows) {
      if (row.client_request_id && pendingIds.has(row.client_request_id))
        continue;
      const key = `receipt:${row.client_request_id ?? row.live_input_id ?? row.id ?? row.text}`;
      if (row.intent === "steer" && row.last_error === "turn_ended") continue;
      if (hasUnknownDeliveryError(row.last_error)) {
        inFlight.push({
          key,
          text: row.text,
          state: "unconfirmed",
          detail: hasRuntimeDrainingError(row.last_error)
            ? "runtime restarting"
            : null,
        });
      } else if (row.status === "delivering") {
        inFlight.push({ key, text: row.text, state: "sending" });
      } else if (row.status === "queued") {
        queued.push({
          key,
          text: row.text,
          state: "queued",
          actions: [cancelAction(row)],
        });
      } else if (
        row.status === "failed" ||
        // A cancel the user asked for needs no tombstone; one with a reason
        // was the system's call and stays visible.
        (row.status === "cancelled" && row.last_error)
      ) {
        failed.push({
          key,
          text: row.text,
          state: "failed",
          detail: row.last_error || null,
        });
      }
    }

    for (const pending of pendingManagedLocalInputs) {
      const key = `pending:${pending.clientRequestId}`;
      const receipt = receiptFor(pending.clientRequestId);
      // A queued send the server is draining right now is mid-delivery, not
      // ambiguous; the reconciler files it under `unknown` only for lack of
      // a closer phase.
      const serverDelivering =
        receipt?.status === "delivering" &&
        !hasUnknownDeliveryError(receipt.last_error);
      if (pending.phase === "unknown" && !serverDelivering) {
        inFlight.push({
          key,
          text: pending.text,
          state: "unconfirmed",
          detail: hasRuntimeDrainingError(receipt?.last_error)
            ? "runtime restarting"
            : null,
          actions: [
            {
              label: "Retry",
              disabled: isSubmitting,
              onClick: () =>
                void outboxActionsRef.current.retry(
                  pending.text,
                  pending.intent,
                  pending.attachments,
                  pending.clientRequestId,
                ),
            },
          ],
        });
      } else if (pending.phase === "queued") {
        queued.push({
          key,
          text: pending.text,
          state: "queued",
          actions:
            receipt?.status === "queued" ? [cancelAction(receipt)] : undefined,
        });
      } else {
        inFlight.push({ key, text: pending.text, state: "sending" });
      }
    }

    if (turnEndedDraft) {
      failed.push({
        key: "turn-ended",
        text: turnEndedDraft,
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

  // Composer header: ember + "Using <tool>" + a mono timer while a turn is
  // active, ember + the server's attention copy when a provider question is
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
  // Same rule as getSessionHeaderState: only an executing activity may claim a
  // tool. A thinking phase keeps the last tool name on the fact, and rendering
  // it there is what made a finished Bash look like running work.
  const activityTool =
    activity.state === "executing" ? activity.tool?.trim() || null : null;
  const composerDelegatedLabel =
    session.session_state.presentation.primary?.key === "delegated_work"
      ? session.session_state.presentation.primary.label || null
      : null;
  const composerElapsedSeconds =
    composerState.tone === "live" && turnStartMs != null
      ? Math.max(0, Math.floor((activityNowMs - turnStartMs) / 1_000))
      : null;
  const composerUsingLabel =
    composerDelegatedLabel ?? (activityTool ? `Using ${activityTool}` : "Working");
  const composerLastTurnMs = Date.parse(
    session.session_state.last_result_at ?? "",
  );
  const composerIdleClock = formatClockTime(composerLastTurnMs);
  const composerObservedClock = formatClockTime(Date.parse(activity.observed_at ?? ""));

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
            <span className="session-chat-queued__text">{turnEndedDraft}</span>
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
            {failedInputs.map((row) => (
              <li
                key={
                  row.client_request_id ??
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
              </li>
            ))}
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
        {isDock ? (
          <div
            className="session-chat-composer__head"
            data-testid="session-chat-composer-head"
          >
            {/* The runtime-evidence strip rides in this row, so the row must
                exist even while the composer is unavailable (a pending
                question, a disconnected control path); only the state label
                yields to the unavailable notice below. */}
            {showComposerUnavailableState ? null : (
              <>
                {composerState.tone === "live" ? (
                  <>
                    <span className="session-ember-dot" aria-hidden="true" />
                    <span className="session-chat-composer__head-label">
                      {composerUsingLabel}
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
                    <span
                      className="session-ember-dot session-ember-dot--attention"
                      aria-hidden="true"
                    />
                    <span className="session-chat-composer__head-label">
                      {composerState.text}
                    </span>
                  </>
                ) : composerState.tone === "unknown" ? (
                  <>
                    <span className="session-unknown-dot" aria-hidden="true" />
                    <span className="session-chat-composer__head-label">
                      Activity uncertain
                    </span>
                    {composerObservedClock ? (
                      <span className="session-chat-composer__head-detail">
                        last observed at {composerObservedClock}
                      </span>
                    ) : null}
                  </>
                ) : (
                  <>
                    <span className="session-cool-dot" aria-hidden="true" />
                    <span className="session-chat-composer__head-label session-chat-composer__head-label--idle">
                      Idle
                    </span>
                    {composerIdleClock ? (
                      <span className="session-chat-composer__head-detail">
                        the last turn ended at {composerIdleClock}
                      </span>
                    ) : null}
                  </>
                )}
              </>
            )}
            {composerHeaderAccessory ? (
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
                        ? "Not confirmed — retry with the same request"
                        : pendingInput.phase === "queued"
                          ? "Queued — server has this request"
                          : "Delivering..."}
                    </span>
                    {pendingInput.phase === "unknown" ? (
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
                            pendingInput.clientRequestId,
                          )
                        }
                      >
                        Retry
                      </Button>
                    ) : null}
                  </div>
                ))
              : null}
            {attachImagesEnabled ? (
              <AttachmentTray
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
                <textarea
                  ref={composerTextareaRef}
                  value={draft}
                  onChange={(e) => {
                    handleDraftChange(e.target.value);
                    autoResizeDockTextarea(e.target);
                  }}
                  onKeyDown={handleKeyDown}
                  placeholder={composerPlaceholder || "Message"}
                  aria-label="Next instruction"
                  disabled={isSubmitting}
                  rows={1}
                />
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
