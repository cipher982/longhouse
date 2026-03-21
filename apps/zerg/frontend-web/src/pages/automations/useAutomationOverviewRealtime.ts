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

type SubscriptionPlan = {
  automationIdsToSubscribe: number[];
  topicsToSubscribe: string[];
  topicsToUnsubscribe: string[];
};

export type AutomationOverviewRealtimeManager = {
  desiredAutomationIdsRef: MutableRefObject<Set<number>>;
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

function planSubscriptionChanges(
  activeIds: Set<number>,
  subscribedAutomationIds: Set<number>,
  pendingAutomationIds: Set<number>,
): SubscriptionPlan {
  const automationIdsToSubscribe: number[] = [];
  const topicsToSubscribe: string[] = [];
  for (const automationId of activeIds) {
    if (!subscribedAutomationIds.has(automationId) && !pendingAutomationIds.has(automationId)) {
      automationIdsToSubscribe.push(automationId);
      topicsToSubscribe.push(`${AUTOMATION_TOPIC_PREFIX}${automationId}`);
    }
  }

  const topicsToUnsubscribe: string[] = [];
  for (const automationId of Array.from(subscribedAutomationIds)) {
    if (!activeIds.has(automationId)) {
      subscribedAutomationIds.delete(automationId);
      topicsToUnsubscribe.push(`${AUTOMATION_TOPIC_PREFIX}${automationId}`);
    }
  }

  return {
    automationIdsToSubscribe,
    topicsToSubscribe,
    topicsToUnsubscribe,
  };
}

function clearLocalSubscriptions(
  pendingSubscriptions: Map<string, PendingSubscription>,
  subscribedAutomationIds: Set<number>,
  desiredAutomationIds: Set<number>,
) {
  clearPendingSubscriptions(pendingSubscriptions);
  subscribedAutomationIds.clear();
  desiredAutomationIds.clear();
}

function replaceSetContents(target: Set<number>, nextValues: Iterable<number>) {
  target.clear();
  for (const value of nextValues) {
    target.add(value);
  }
}

function sendUnsubscribeTopics(
  topics: string[],
  nextMessageId: () => string,
  sendMessage: (message: WebSocketMessage) => void,
) {
  if (topics.length === 0) {
    return;
  }

  const messageId = nextMessageId();
  sendMessage(createEnvelope("unsubscribe", "system", { topics, message_id: messageId }, messageId));
}

export function useAutomationOverviewRealtimeManager(): AutomationOverviewRealtimeManager {
  const desiredAutomationIdsRef = useRef<Set<number>>(new Set());
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
      let needsReconcile = false;
      pending.automationIds.forEach((automationId) => {
        subscribedAutomationIdsRef.current.add(automationId);
        if (!desiredAutomationIdsRef.current.has(automationId)) {
          needsReconcile = true;
        }
      });
      if (needsReconcile) {
        retrySubscriptions();
      }
    } else {
      console.error("[WS] Subscription failed for topics:", pending.topics);
    }

    return true;
  }, [retrySubscriptions]);

  return {
    desiredAutomationIdsRef,
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
    desiredAutomationIdsRef,
    nextMessageId,
    pendingSubscriptionsRef,
    retrySubscriptions,
    subscribedAutomationIdsRef,
    subscriptionRevision,
  } = manager;

  useEffect(() => {
    if (!enabled) {
      const staleTopics = Array.from(subscribedAutomationIdsRef.current).map(
        (automationId) => `${AUTOMATION_TOPIC_PREFIX}${automationId}`,
      );
      clearLocalSubscriptions(
        pendingSubscriptionsRef.current,
        subscribedAutomationIdsRef.current,
        desiredAutomationIdsRef.current,
      );
      if (connectionStatus === ConnectionStatus.CONNECTED) {
        sendUnsubscribeTopics(staleTopics, nextMessageId, sendMessageRef.current);
      }
      return;
    }

    const activeIds = new Set(automationIds);
    replaceSetContents(desiredAutomationIdsRef.current, activeIds);

    if (connectionStatus !== ConnectionStatus.CONNECTED) {
      return;
    }

    const pendingAutomationIds = new Set<number>();
    pendingSubscriptionsRef.current.forEach((pending) => {
      pending.automationIds.forEach((automationId) => pendingAutomationIds.add(automationId));
    });

    const { automationIdsToSubscribe, topicsToSubscribe, topicsToUnsubscribe } = planSubscriptionChanges(
      activeIds,
      subscribedAutomationIdsRef.current,
      pendingAutomationIds,
    );

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

    sendUnsubscribeTopics(topicsToUnsubscribe, nextMessageId, sendMessageRef.current);
  }, [
    automationIds,
    connectionStatus,
    desiredAutomationIdsRef,
    enabled,
    nextMessageId,
    pendingSubscriptionsRef,
    retrySubscriptions,
    sendMessageRef,
    subscribedAutomationIdsRef,
    subscriptionRevision,
  ]);

  useEffect(() => {
    const pendingSubscriptions = pendingSubscriptionsRef.current;
    const subscribedAutomationIds = subscribedAutomationIdsRef.current;
    const desiredAutomationIds = desiredAutomationIdsRef.current;

    return () => {
      clearLocalSubscriptions(pendingSubscriptions, subscribedAutomationIds, desiredAutomationIds);
    };
  }, [desiredAutomationIdsRef, pendingSubscriptionsRef, subscribedAutomationIdsRef]);
}
