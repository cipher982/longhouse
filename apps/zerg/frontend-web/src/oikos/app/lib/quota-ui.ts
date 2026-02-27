import type { UserUsageResponse } from "./usage";

export interface QuotaUiState {
  blocked: boolean;
  helperText: string | null;
  placeholderOverride: string | null;
}

export function getResetCountdownLabel(): string {
  const now = new Date();
  const nextUtcMidnightMs = Date.UTC(
    now.getUTCFullYear(),
    now.getUTCMonth(),
    now.getUTCDate() + 1,
    0,
    0,
    0,
  );
  const remainingMs = Math.max(0, nextUtcMidnightMs - now.getTime());
  const totalMinutes = Math.floor(remainingMs / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return `${hours}h ${minutes}m`;
}

export function formatUsd(value: number): string {
  if (value <= 0) return "$0.00";
  if (value < 0.01) return "<$0.01";
  return `$${value.toFixed(2)}`;
}

export function getQuotaUiState(usage?: UserUsageResponse): QuotaUiState {
  const status = usage?.limit.status;

  if (status === "exceeded") {
    const reset = getResetCountdownLabel();
    const helper = `Shared quota reached. Resets in ${reset} (00:00 UTC). Add your provider key in Settings to continue now.`;
    return {
      blocked: true,
      helperText: helper,
      placeholderOverride: "Shared quota reached. Add your provider key in Settings or wait for reset.",
    };
  }

  if (status === "warning" && usage) {
    return {
      blocked: false,
      helperText: `Low shared quota: ${formatUsd(usage.limit.remaining_usd)} remaining today.`,
      placeholderOverride: null,
    };
  }

  return {
    blocked: false,
    helperText: null,
    placeholderOverride: null,
  };
}
