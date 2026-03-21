import { useCallback, useEffect, useRef, useState, type MutableRefObject } from "react";
import { useLatest } from "../../hooks/useLatest";
import { ConnectionStatus, createEnvelope, type WebSocketMessage } from "../../lib/useWebSocket";

const AUTOMATION_TOPIC_PREFIX = "automation:";
const SUBSCRIPTION_TIMEOUT_MS = 5000;

type AutomationControlMessage = WebSocketMessage & {
  data?: unknown;
  message_id?: string;
};

type PendingSubscription = {
  topics: string[];
  timeoutId: number;
  automationIds: number[];
};

export type AutomationOverviewRealtimeManager = {
  subscriptionRevision: number;
  handleConnect: () => void;
  handleControlMessage: (message: AutomationControlMessage) => boolean;
  nextMessageId: () => string;
  pendingSubscriptionsRef: MutableRefObject<Map<string, PendingSubscription>>;
  retrySubscriptions: () => void;
  subscribedAutomationIdsRef: MutableRefObject<Set<number>>;
};

function extractMessageId(message: AutomationControlMessage): string {
  if (typeof message.message_id === "string" && message.message_id.length > 0) {
    return message.message_id;
  }

  if (typeof message.data !== "object" || message.data === null) {
    return "";
  }

  const messageData = message.data as Record<string, unknown>;
  return typeof messageData.message_id === "string" ? messageData.message_id : "";
}

function clearPendingSubscriptions(pendingSubscriptions: Map<string, PendingSubscription>) {
  pendingSubscriptions.forEach((pending) => {
    clearTimeout(pending.timeoutId);
  });
  pendingSubscriptions.clear();
}

export function useAutomationOverviewRealtimeManager(): AutomationOverviewRealtimeManager {
  const subscribedAutomationIdsRef = useRef<Set<number>>(new Set());
  const pendingSubscriptionsRef = useRef<Map<string, PendingSubscription>>(new Map());
  const messageIdCounterRef = useRef(0);
  const [subscriptionRevision, setSubscriptionRevision] = useState(0);

  const nextMessageId = useCallback(() => {
    messageIdCounterRef.current += 1;
    return `automations-${Date.now()}-${messageIdCounterRef.current}`;
  }, []);

  const retrySubscriptions = useCallback(() => {
    setSubscriptionRevision((value) => value + 1);
  }, []);

  const handleConnect = useCallback(() => {
    subscribedAutomationIdsRef.current.clear();
    clearPendingSubscriptions(pendingSubscriptionsRef.current);
    retrySubscriptions();
  }, [retrySubscriptions]);

  const handleControlMessage = useCallback((message: AutomationControlMessage) => {
    if (message.type !== "subscribe_ack" && message.type !== "subscribe_error") {
      return false;
    }

    const messageId = extractMessageId(message);
    if (!messageId) {
      return true;
    }

    const pending = pendingSubscriptionsRef.current.get(messageId);
    if (!pending) {
      return true;
    }

    clearTimeout(pending.timeoutId);
    pendingSubscriptionsRef.current.delete(messageId);

    if (message.type === "subscribe_ack") {
      pending.automationIds.forEach((automationId) => {
        subscribedAutomationIdsRef.current.add(automationId);
      });
    } else {
      console.error("[WS] Subscription failed for topics:", pending.topics);
      retrySubscriptions();
    }

    return true;
  }, [retrySubscriptions]);

  return {
    subscriptionRevision,
    handleConnect,
    handleControlMessage,
    nextMessageId,
    pendingSubscriptionsRef,
    retrySubscriptions,
    subscribedAutomationIdsRef,
  };
}

type UseAutomationOverviewRealtimeSubscriptionsOptions = {
  automationIds: number[];
  connectionStatus: ConnectionStatus;
  enabled: boolean;
  manager: AutomationOverviewRealtimeManager;
  sendMessage: (message: WebSocketMessage) => void;
};

export function useAutomationOverviewRealtimeSubscriptions({
  automationIds,
  connectionStatus,
  enabled,
  manager,
  sendMessage,
}: UseAutomationOverviewRealtimeSubscriptionsOptions) {
  const sendMessageRef = useLatest(sendMessage);
  const {
    nextMessageId,
    pendingSubscriptionsRef,
    retrySubscriptions,
    subscribedAutomationIdsRef,
    subscriptionRevision,
  } = manager;

  useEffect(() => {
    if (!enabled) {
      return;
    }
    if (connectionStatus !== ConnectionStatus.CONNECTED) {
      return;
    }

    const activeIds = new Set(automationIds);
    const pendingAutomationIds = new Set<number>();
    pendingSubscriptionsRef.current.forEach((pending) => {
      pending.automationIds.forEach((automationId) => pendingAutomationIds.add(automationId));
    });

    const topicsToSubscribe: string[] = [];
    const automationIdsToSubscribe: number[] = [];
    for (const automationId of activeIds) {
      if (!subscribedAutomationIdsRef.current.has(automationId) && !pendingAutomationIds.has(automationId)) {
        topicsToSubscribe.push(`${AUTOMATION_TOPIC_PREFIX}${automationId}`);
        automationIdsToSubscribe.push(automationId);
      }
    }

    const topicsToUnsubscribe: string[] = [];
    for (const automationId of Array.from(subscribedAutomationIdsRef.current)) {
      if (!activeIds.has(automationId)) {
        subscribedAutomationIdsRef.current.delete(automationId);
        topicsToUnsubscribe.push(`${AUTOMATION_TOPIC_PREFIX}${automationId}`);
      }
    }

    if (topicsToSubscribe.length > 0) {
      const messageId = nextMessageId();
      const timeoutId = window.setTimeout(() => {
        if (pendingSubscriptionsRef.current.has(messageId)) {
          console.warn("[WS] Subscription timeout for topics:", topicsToSubscribe);
          pendingSubscriptionsRef.current.delete(messageId);
          retrySubscriptions();
        }
      }, SUBSCRIPTION_TIMEOUT_MS);

      pendingSubscriptionsRef.current.set(messageId, {
        topics: topicsToSubscribe,
        timeoutId,
        automationIds: automationIdsToSubscribe,
      });

      sendMessageRef.current(
        createEnvelope("subscribe", "system", { topics: topicsToSubscribe, message_id: messageId }, messageId),
      );
    }

    if (topicsToUnsubscribe.length > 0) {
      const messageId = nextMessageId();
      sendMessageRef.current(
        createEnvelope("unsubscribe", "system", { topics: topicsToUnsubscribe, message_id: messageId }, messageId),
      );
    }
  }, [
    automationIds,
    connectionStatus,
    enabled,
    nextMessageId,
    pendingSubscriptionsRef,
    retrySubscriptions,
    sendMessageRef,
    subscribedAutomationIdsRef,
    subscriptionRevision,
  ]);

  useEffect(() => {
    if (enabled) {
      return;
    }

    clearPendingSubscriptions(pendingSubscriptionsRef.current);

    if (subscribedAutomationIdsRef.current.size === 0) {
      return;
    }

    const topics = Array.from(subscribedAutomationIdsRef.current).map(
      (automationId) => `${AUTOMATION_TOPIC_PREFIX}${automationId}`,
    );
    const messageId = nextMessageId();
    sendMessageRef.current(
      createEnvelope("unsubscribe", "system", { topics, message_id: messageId }, messageId),
    );
    subscribedAutomationIdsRef.current.clear();
  }, [enabled, nextMessageId, pendingSubscriptionsRef, sendMessageRef, subscribedAutomationIdsRef]);

  useEffect(() => {
    const pendingSubscriptions = pendingSubscriptionsRef.current;
    const subscribedAutomationIds = subscribedAutomationIdsRef.current;
    const sendMessageLatest = sendMessageRef.current;

    return () => {
      clearPendingSubscriptions(pendingSubscriptions);

      if (subscribedAutomationIds.size === 0) {
        return;
      }

      const topics = Array.from(subscribedAutomationIds).map((automationId) => `${AUTOMATION_TOPIC_PREFIX}${automationId}`);
      const messageId = nextMessageId();
      sendMessageLatest(createEnvelope("unsubscribe", "system", { topics, message_id: messageId }, messageId));
      subscribedAutomationIds.clear();
    };
  }, [nextMessageId, pendingSubscriptionsRef, sendMessageRef, subscribedAutomationIdsRef]);
}
