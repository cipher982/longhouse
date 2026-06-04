import { useEffect, useMemo } from "react";
import type { TimelineSessionCard } from "../services/api/agents";
import { usePageMeta } from "./usePageMeta";
import { useDocumentVisible } from "./useDocumentVisible";

type NavigatorWithBadge = Navigator & {
  setAppBadge?: (contents?: number) => Promise<void>;
  clearAppBadge?: () => Promise<void>;
};

function attentionCount(sessions: TimelineSessionCard[]): number {
  return sessions.filter((thread) => thread.head.runtime_display?.needs_attention).length;
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
    return () => {
      setBrowserBadge(0);
    };
  }, []);

  return documentVisible ? 0 : count;
}
