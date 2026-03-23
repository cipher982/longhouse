import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../services/api/base";
import { useLoopPushNotifications } from "../useLoopPushNotifications";
import {
  deleteLoopPushSubscription,
  fetchLoopPushConfig,
  registerLoopPushSubscription,
} from "../../services/api/oikos";

vi.mock("../../services/api/oikos", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api/oikos")>();
  return {
    ...actual,
    fetchLoopPushConfig: vi.fn(),
    registerLoopPushSubscription: vi.fn(),
    deleteLoopPushSubscription: vi.fn(),
  };
});

const fetchLoopPushConfigMock = vi.mocked(fetchLoopPushConfig);
const registerLoopPushSubscriptionMock = vi.mocked(registerLoopPushSubscription);
const deleteLoopPushSubscriptionMock = vi.mocked(deleteLoopPushSubscription);

describe("useLoopPushNotifications", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    fetchLoopPushConfigMock.mockResolvedValue({
      enabled: true,
      vapidPublicKey: "test-vapid",
    });
    registerLoopPushSubscriptionMock.mockResolvedValue();

    Object.defineProperty(window, "PushManager", {
      configurable: true,
      value: function PushManager() {},
    });
    Object.defineProperty(window, "Notification", {
      configurable: true,
      value: {
        permission: "granted",
        requestPermission: vi.fn().mockResolvedValue("granted"),
      },
    });
  });

  it("still unsubscribes locally when the backend already revoked the push subscription", async () => {
    const unsubscribeMock = vi.fn().mockResolvedValue(true);
    const subscription = {
      endpoint: "https://push.example/subscription",
      toJSON: () => ({ endpoint: "https://push.example/subscription" }),
      unsubscribe: unsubscribeMock,
    };
    const registration = {
      scope: "https://david010.longhouse.ai/loop/",
      pushManager: {
        getSubscription: vi.fn().mockResolvedValue(subscription),
        subscribe: vi.fn(),
      },
    };

    Object.defineProperty(window.navigator, "serviceWorker", {
      configurable: true,
      value: {
        ready: Promise.resolve(registration),
      },
    });

    deleteLoopPushSubscriptionMock.mockRejectedValue(
      new ApiError({
        url: "/api/oikos/push-subscriptions",
        status: 404,
        body: { detail: "Subscription not found" },
      }),
    );

    const { result } = renderHook(() => useLoopPushNotifications({ isInstalled: true }));

    await waitFor(() => {
      expect(result.current.isEnabled).toBe(true);
    });

    await act(async () => {
      await expect(result.current.disable()).resolves.toBe(true);
    });

    expect(deleteLoopPushSubscriptionMock).toHaveBeenCalledWith("https://push.example/subscription");
    expect(unsubscribeMock).toHaveBeenCalledTimes(1);
    expect(result.current.isEnabled).toBe(false);
  });
});
