import { useEffect, useMemo } from "react";
import type { TimelineSessionCard } from "../services/api/agents";
import { usePageMeta } from "./usePageMeta";
import { useDocumentVisible } from "./useDocumentVisible";

type NavigatorWithBadge = Navigator & {
  setAppBadge?: (contents?: number) => Promise<void>;
  clearAppBadge?: () => Promise<void>;
};

function attentionCount(sessions: TimelineSessionCard[]): number {
  return sessions.filter((thread) => thread.head.session_state.pending_interaction != null).length;
}

function attentionTitle(count: number): string {
  if (count <= 1) {
    return "● Blocked · Longhouse";
  }
  return `● ${count} blocked · Longhouse`;
}

function setBrowserBadge(count: number) {
  if (typeof navigator === "undefined") {
    return;
  }
  const badgeNavigator = navigator as NavigatorWithBadge;
  if (count > 0 && typeof badgeNavigator.setAppBadge === "function") {
    void badgeNavigator.setAppBadge(count).catch(() => {});
    return;
  }
  if (count === 0 && typeof badgeNavigator.clearAppBadge === "function") {
    void badgeNavigator.clearAppBadge().catch(() => {});
  }
}

function setFaviconAttention(active: boolean) {
  if (typeof document === "undefined") {
    return;
  }
  const links = document.querySelectorAll<HTMLLinkElement>('link[rel="icon"]');
  for (const link of links) {
    const href = link.getAttribute("href") ?? "";
    const base = href.split("?")[0];
    link.href = active ? `${base}?v=3&attention=1` : `${base}?v=3`;
  }
}

export function useAmbientSessionAttentionCue(
  sessions: TimelineSessionCard[],
  options: { enabled?: boolean } = {},
): number {
  const documentVisible = useDocumentVisible();
  const enabled = options.enabled !== false;
  const count = useMemo(() => attentionCount(sessions), [sessions]);
  const shouldCue = enabled && !documentVisible && count > 0;

  usePageMeta({
    title: shouldCue ? attentionTitle(count) : "Longhouse",
  });

  useEffect(() => {
    if (shouldCue) {
      setBrowserBadge(count);
      return;
    }
    setBrowserBadge(0);
  }, [count, shouldCue]);

  useEffect(() => {
    setFaviconAttention(shouldCue);
    return () => {
      setFaviconAttention(false);
    };
  }, [shouldCue]);

  useEffect(() => {
    return () => {
      setBrowserBadge(0);
    };
  }, []);

  return documentVisible ? 0 : count;
}
