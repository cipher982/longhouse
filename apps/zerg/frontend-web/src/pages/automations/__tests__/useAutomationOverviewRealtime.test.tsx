import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConnectionStatus, type WebSocketMessage } from "../../../lib/useWebSocket";
import {
  useAutomationOverviewRealtimeManager,
  useAutomationOverviewRealtimeSubscriptions,
} from "../useAutomationOverviewRealtime";

type HookProps = {
  automationIds: number[];
  connectionStatus: ConnectionStatus;
  enabled: boolean;
  sendMessage: (message: WebSocketMessage) => void;
};

function renderRealtimeHook(initialProps: HookProps) {
  return renderHook((props: HookProps) => {
    const manager = useAutomationOverviewRealtimeManager();
    useAutomationOverviewRealtimeSubscriptions({
      automationIds: props.automationIds,
      connectionStatus: props.connectionStatus,
      enabled: props.enabled,
      manager,
      sendMessage: props.sendMessage,
    });
    return manager;
  }, { initialProps });
}

function getSentMessages(sendMessage: ReturnType<typeof vi.fn>): WebSocketMessage[] {
  return sendMessage.mock.calls.map(([message]) => message as WebSocketMessage);
}

function getLastMessageId(sendMessage: ReturnType<typeof vi.fn>): string {
  const lastMessage = getSentMessages(sendMessage).at(-1) as WebSocketMessage & {
    data?: { message_id?: string };
  };
  return lastMessage.data?.message_id || "";
}

describe("useAutomationOverviewRealtimeSubscriptions", () => {
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("subscribes visible automation IDs once while the ack is still pending", async () => {
    const sendMessage = vi.fn();
    const { rerender } = renderRealtimeHook({
      automationIds: [11, 22],
      connectionStatus: ConnectionStatus.CONNECTED,
      enabled: true,
      sendMessage,
    });

    expect(sendMessage).toHaveBeenCalledTimes(1);
    expect(getSentMessages(sendMessage)[0]).toMatchObject({
      type: "subscribe",
      data: { topics: ["automation:11", "automation:22"] },
    });

    rerender({
      automationIds: [11, 22],
      connectionStatus: ConnectionStatus.CONNECTED,
      enabled: true,
      sendMessage,
    });

    expect(sendMessage).toHaveBeenCalledTimes(1);
  });

  it("unsubscribes stale IDs when a late subscribe ack lands after the desired set changed", async () => {
    const sendMessage = vi.fn();
    const { result, rerender } = renderRealtimeHook({
      automationIds: [11],
      connectionStatus: ConnectionStatus.CONNECTED,
      enabled: true,
      sendMessage,
    });

    expect(sendMessage).toHaveBeenCalledTimes(1);
    const messageId = getLastMessageId(sendMessage);

    rerender({
      automationIds: [],
      connectionStatus: ConnectionStatus.CONNECTED,
      enabled: true,
      sendMessage,
    });

    expect(sendMessage).toHaveBeenCalledTimes(1);

    act(() => {
      result.current.handleControlMessage({
        type: "subscribe_ack",
        data: { message_id: messageId },
      });
    });

    expect(sendMessage).toHaveBeenCalledTimes(2);
    expect(getSentMessages(sendMessage)[1]).toMatchObject({
      type: "unsubscribe",
      data: { topics: ["automation:11"] },
    });
  });

  it("retries once when a pending subscription times out", async () => {
    vi.useFakeTimers();
    const sendMessage = vi.fn();
    renderRealtimeHook({
      automationIds: [33],
      connectionStatus: ConnectionStatus.CONNECTED,
      enabled: true,
      sendMessage,
    });

    expect(sendMessage).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(5000);
    });

    expect(sendMessage).toHaveBeenCalledTimes(2);
    expect(getSentMessages(sendMessage)[1]).toMatchObject({
      type: "subscribe",
      data: { topics: ["automation:33"] },
    });
  });

  it("does not immediately retry on subscribe_error", async () => {
    const sendMessage = vi.fn();
    const { result } = renderRealtimeHook({
      automationIds: [44],
      connectionStatus: ConnectionStatus.CONNECTED,
      enabled: true,
      sendMessage,
    });

    expect(sendMessage).toHaveBeenCalledTimes(1);
    const messageId = getLastMessageId(sendMessage);

    act(() => {
      result.current.handleControlMessage({
        type: "subscribe_error",
        data: { message_id: messageId },
      });
    });

    expect(sendMessage).toHaveBeenCalledTimes(1);
  });
});
