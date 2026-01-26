/**
 * UsageWidget - Displays user's LLM usage and budget status
 *
 * Shows:
 * - Progress bar of daily budget usage
 * - Cost spent today
 * - Remaining budget
 * - Warning when approaching limit
 */

import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import config from "../lib/config";
import { ZapIcon } from "./icons";

// Types matching backend schema
interface TokenBreakdown {
  prompt: number | null;
  completion: number | null;
  total: number;
}

interface UsageLimit {
  daily_cost_cents: number;
  used_percent: number;
  remaining_usd: number;
  status: "ok" | "warning" | "exceeded" | "unlimited";
}

interface UserUsageResponse {
  period: "today" | "7d" | "30d";
  tokens: TokenBreakdown;
  cost_usd: number;
  courses: number;
  limit: UsageLimit;
}

// Fetch user usage from API
async function fetchUserUsage(period: "today" | "7d" | "30d" = "today"): Promise<UserUsageResponse> {
  const response = await fetch(`${config.apiBaseUrl}/users/me/usage?period=${period}`, {
    credentials: "include",
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch usage: ${response.status}`);
  }

  return response.json();
}

// Format token count with commas
function formatTokens(tokens: number): string {
  return tokens.toLocaleString();
}

// Format cost with appropriate precision
function formatCost(cost: number): string {
  if (cost === 0) {
    return "$0.00";
  }
  if (cost >= 0.01) {
    return `$${cost.toFixed(2)}`;
  }
  // For very small amounts, show enough precision to be meaningful
  return `<$0.01`;
}

// Get progress bar color based on percentage
function getProgressColor(percent: number): string {
  if (percent >= 95) return "#ef4444"; // Red
  if (percent >= 80) return "#f97316"; // Orange
  if (percent >= 60) return "#eab308"; // Yellow
  return "#22c55e"; // Green
}

// Local storage key for warning toast debounce
const WARNING_TOAST_KEY = "usage_warning_shown_date";

export default function UsageWidget() {
  const warningShownRef = useRef(false);

  const { data: usage, isLoading, error } = useQuery({
    queryKey: ["user-usage", "today"],
    queryFn: () => fetchUserUsage("today"),
    refetchInterval: 60000, // Refresh every 60 seconds
    staleTime: 30000, // Consider stale after 30 seconds
  });

  // Handle 80% warning toast
  useEffect(() => {
    if (!usage) return;
    if (usage.limit.status !== "warning") return;
    if (warningShownRef.current) return;

    // Check localStorage to avoid showing multiple times per day
    const today = new Date().toISOString().split("T")[0];
    let lastWarningDate: string | null = null;
    try {
      lastWarningDate = localStorage.getItem(WARNING_TOAST_KEY);
    } catch {
      // localStorage may be unavailable (e.g. private mode / blocked storage)
      lastWarningDate = null;
    }

    if (lastWarningDate === today) {
      warningShownRef.current = true;
      return;
    }

    // Show warning toast
    toast(
      (t) => (
        <div className="usage-warning-toast">
          <strong>Approaching daily limit</strong>
          <p>
            You've used {usage.limit.used_percent.toFixed(0)}% of your daily LLM budget.
            <br />
            {formatCost(usage.limit.remaining_usd)} remaining.
          </p>
          <button
            onClick={() => toast.dismiss(t.id)}
            className="usage-warning-dismiss"
          >
            Dismiss
          </button>
        </div>
      ),
      {
        duration: 10000,
        position: "top-right",
        style: {
          background: "#fef3c7",
          border: "1px solid #f59e0b",
          padding: "12px 16px",
        },
      }
    );

    // Mark as shown for today
    try {
      localStorage.setItem(WARNING_TOAST_KEY, today);
    } catch {
      // Ignore storage errors; toast will still be debounced by ref for this session.
    }
    warningShownRef.current = true;
  }, [usage]);

  // Loading state
  if (isLoading) {
    return (
      <div className="usage-widget usage-widget-loading">
        <div className="usage-widget-header">
          <div className="usage-widget-icon-wrapper">
            <ZapIcon className="usage-widget-icon" />
          </div>
          <span className="usage-widget-title">Today's Usage</span>
        </div>
        <div className="usage-widget-loading-text">Loading...</div>
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <div className="usage-widget usage-widget-error">
        <div className="usage-widget-header">
          <div className="usage-widget-icon-wrapper">
            <ZapIcon className="usage-widget-icon" />
          </div>
          <span className="usage-widget-title">Today's Usage</span>
        </div>
        <div className="usage-widget-error-text">Unable to load usage data</div>
      </div>
    );
  }

  // No data
  if (!usage) {
    return null;
  }

  const { tokens, cost_usd, courses, limit } = usage;
  const isUnlimited = limit.status === "unlimited";
  const progressColor = getProgressColor(limit.used_percent);

  return (
    <div className={`usage-widget usage-widget-${limit.status}`}>
      <div className="usage-widget-header">
        <div className="usage-widget-icon-wrapper">
          <ZapIcon className="usage-widget-icon" />
        </div>
        <span className="usage-widget-title">Today's Usage</span>
      </div>

      {/* Progress bar (only if limit is set) */}
      {!isUnlimited && (
        <div className="usage-widget-progress-container">
          <div className="usage-widget-progress-bar">
            <div
              className="usage-widget-progress-fill"
              style={{
                width: `${Math.min(100, limit.used_percent)}%`,
                backgroundColor: progressColor,
              }}
            />
          </div>
          <div className="usage-widget-progress-label">
            {limit.used_percent.toFixed(0)}%
          </div>
        </div>
      )}

      {/* Cost summary */}
      <div className="usage-widget-summary">
        {isUnlimited ? (
          <div className="usage-widget-cost">
            <span className="usage-widget-cost-value">{formatCost(cost_usd)}</span>
            <span className="usage-widget-cost-label">spent today</span>
          </div>
        ) : (
          <div className="usage-widget-cost">
            <span className="usage-widget-cost-value">{formatCost(cost_usd)}</span>
            <span className="usage-widget-cost-label">
              of {formatCost(limit.daily_cost_cents / 100)} daily limit
            </span>
          </div>
        )}
      </div>

      {/* Token and run count */}
      <div className="usage-widget-details">
        <span className="usage-widget-detail">
          {formatTokens(tokens.total)} tokens
        </span>
        <span className="usage-widget-detail-separator">·</span>
        <span className="usage-widget-detail">
          {courses} course{courses !== 1 ? "s" : ""}
        </span>
      </div>

      {/* Warning banner */}
      {limit.status === "warning" && (
        <div className="usage-widget-warning">
          Approaching daily limit
        </div>
      )}

      {/* Exceeded banner */}
      {limit.status === "exceeded" && (
        <div className="usage-widget-exceeded">
          Daily limit reached
        </div>
      )}
    </div>
  );
}
