import { formatRelativeTime } from "@/shared/lib/dateUtils";
import { resolveSessionRuntimeState } from "./sessionRuntime";

// ---------------------------------------------------------------------------
// Runtime display helpers
// ---------------------------------------------------------------------------

export function getRuntimeMetaLabel(
  runtime: ReturnType<typeof resolveSessionRuntimeState>,
  relativeNowMs?: number,
): string | null {
  if (runtime.lastLiveAt && runtime.confidence === "stale") {
    return `Updated ${formatRelativeTime(runtime.lastLiveAt, relativeNowMs)}`;
  }
  return null;
}

export function getRuntimeOutcomeLabel(
  runtime: ReturnType<typeof resolveSessionRuntimeState>,
): string {
  return runtime.stateFacts.presentation.primary?.label ?? "Activity unknown";
}

export interface RuntimeDisplayCopy {
  headline: string;
  detail: string | null;
}

export function getRuntimeDisplayCopy(
  runtime: ReturnType<typeof resolveSessionRuntimeState>,
): RuntimeDisplayCopy {
  return {
    headline:
      runtime.stateFacts.presentation.primary?.label ?? "Activity unknown",
    detail: runtime.stateFacts.presentation.transcript?.label ?? null,
  };
}
