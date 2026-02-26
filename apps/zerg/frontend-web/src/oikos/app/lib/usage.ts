import { toAbsoluteUrl } from "../../lib/config";

export interface TokenBreakdown {
  prompt: number | null;
  completion: number | null;
  total: number;
}

export interface UsageLimit {
  daily_cost_cents: number;
  used_percent: number;
  remaining_usd: number;
  status: "ok" | "warning" | "exceeded" | "unlimited";
}

export interface UserUsageResponse {
  period: "today" | "7d" | "30d";
  tokens: TokenBreakdown;
  cost_usd: number;
  runs: number;
  limit: UsageLimit;
}

export async function fetchUserUsage(period: "today" | "7d" | "30d" = "today"): Promise<UserUsageResponse> {
  const response = await fetch(toAbsoluteUrl(`/api/users/me/usage?period=${period}`), {
    credentials: "include",
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch usage: ${response.status}`);
  }

  return response.json();
}
