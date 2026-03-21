import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError } from "../services/api/base";
import {
  deleteLoopPushSubscription,
  fetchLoopPushConfig,
  registerLoopPushSubscription,
  type LoopPushConfig,
} from "../services/api/oikos";

const LOOP_INSTALL_ID_KEY = "longhouse.loop.install_id";

function isIos(): boolean {
  if (typeof navigator === "undefined") return false;
  return /iphone|ipad|ipod/i.test(navigator.userAgent);
}

function createInstallId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `loop-${Date.now()}`;
}

function getInstallId(): string {
  if (typeof window === "undefined") return createInstallId();
  const existing = window.localStorage.getItem(LOOP_INSTALL_ID_KEY);
  if (existing) return existing;
  const next = createInstallId();
  window.localStorage.setItem(LOOP_INSTALL_ID_KEY, next);
  return next;
}

function urlBase64ToUint8Array(base64String: string): BufferSource {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let index = 0; index < rawData.length; index += 1) {
    outputArray[index] = rawData.charCodeAt(index);
  }
  return outputArray as BufferSource;
}

async function getLoopRegistration(): Promise<ServiceWorkerRegistration | null> {
  if (!("serviceWorker" in navigator)) return null;
  const registration = await navigator.serviceWorker.ready;
  return registration.scope.includes("/loop") ? registration : null;
}

export function useLoopPushNotifications({ isInstalled }: { isInstalled: boolean }) {
  const [config, setConfig] = useState<LoopPushConfig | null>(null);
  const [isEnabled, setIsEnabled] = useState(false);
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [permission, setPermission] = useState<NotificationPermission>(() =>
    typeof Notification === "undefined" ? "default" : Notification.permission,
  );

  const supported = useMemo(() => {
    if (typeof window === "undefined") return false;
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      return false;
    }
    if (isIos() && !isInstalled) return false;
    return true;
  }, [isInstalled]);

  const syncExistingSubscription = useCallback(async () => {
    if (!supported) return false;
    const nextConfig = config ?? (await fetchLoopPushConfig());
    setConfig(nextConfig);
    if (!nextConfig.enabled || !nextConfig.vapidPublicKey) {
      setIsEnabled(false);
      return false;
    }

    const registration = await getLoopRegistration();
    if (!registration) return false;
    const subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      setIsEnabled(false);
      return false;
    }

    await registerLoopPushSubscription({
      subscription: subscription.toJSON(),
      installId: getInstallId(),
      userAgent: navigator.userAgent,
    });
    setIsEnabled(true);
    return true;
  }, [config, supported]);

  useEffect(() => {
    let cancelled = false;
    if (!supported) return;

    (async () => {
      try {
        const nextConfig = await fetchLoopPushConfig();
        if (cancelled) return;
        setConfig(nextConfig);
        if (!nextConfig.enabled || Notification.permission !== "granted") return;
        const synced = await syncExistingSubscription();
        if (!cancelled) {
          setIsEnabled(synced);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to initialize Loop notifications.");
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [supported, syncExistingSubscription]);

  const enable = useCallback(async () => {
    if (!supported) return false;
    setIsBusy(true);
    setError(null);
    try {
      const nextConfig = config ?? (await fetchLoopPushConfig());
      setConfig(nextConfig);
      if (!nextConfig.enabled || !nextConfig.vapidPublicKey) {
        throw new Error("Loop notifications are not enabled on this instance.");
      }

      const nextPermission = await Notification.requestPermission();
      setPermission(nextPermission);
      if (nextPermission !== "granted") {
        setIsEnabled(false);
        return false;
      }

      const registration = await getLoopRegistration();
      if (!registration) {
        throw new Error("Loop service worker is not ready yet.");
      }

      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(nextConfig.vapidPublicKey),
        });
      }

      await registerLoopPushSubscription({
        subscription: subscription.toJSON(),
        installId: getInstallId(),
        userAgent: navigator.userAgent,
      });
      setIsEnabled(true);
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to enable Loop notifications.");
      return false;
    } finally {
      setIsBusy(false);
    }
  }, [config, supported]);

  const disable = useCallback(async () => {
    if (!supported) return false;
    setIsBusy(true);
    setError(null);
    try {
      const registration = await getLoopRegistration();
      if (!registration) return false;
      const subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        setIsEnabled(false);
        return true;
      }
      try {
        await deleteLoopPushSubscription(subscription.endpoint);
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 404) {
          throw err;
        }
      }
      await subscription.unsubscribe();
      setIsEnabled(false);
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to disable Loop notifications.");
      return false;
    } finally {
      setIsBusy(false);
    }
  }, [supported]);

  return {
    supported,
    enabledInBackend: Boolean(config?.enabled),
    permission,
    isEnabled,
    isBusy,
    error,
    canEnable: supported && Boolean(config?.enabled) && permission !== "denied" && !isEnabled,
    canDisable: supported && isEnabled,
    enable,
    disable,
  };
}
