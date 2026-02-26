import { Link } from "react-router-dom";
import type { UserUsageResponse } from "../lib/usage";
import { getQuotaUiState } from "../lib/quota-ui";

interface QuotaPanelProps {
  usage?: UserUsageResponse;
  isLoading: boolean;
  isError: boolean;
}

function formatUsd(value: number): string {
  if (value <= 0) return "$0.00";
  if (value < 0.01) return "<$0.01";
  return `$${value.toFixed(2)}`;
}

export function QuotaPanel({ usage, isLoading, isError }: QuotaPanelProps) {
  if (isLoading) {
    return <div className="quota-panel quota-panel--loading">Checking quota…</div>;
  }

  if (isError || !usage) {
    return <div className="quota-panel quota-panel--error">Usage unavailable</div>;
  }

  const { limit, runs, cost_usd } = usage;
  const status = limit.status;
  const percent = Math.min(100, Math.max(0, limit.used_percent));
  const dailyLimitUsd = limit.daily_cost_cents / 100;
  const quotaUi = getQuotaUiState(usage);

  return (
    <div className={`quota-panel quota-panel--${status}`} data-testid="quota-panel">
      <div className="quota-panel__top">
        <span className="quota-panel__label">Shared Pool</span>
        <span className="quota-panel__status">
          {status === "exceeded" ? "Blocked" : status === "warning" ? "Near limit" : "Healthy"}
        </span>
      </div>
      {status !== "unlimited" && (
        <div className="quota-panel__bar" aria-label="Daily quota usage">
          <div className="quota-panel__bar-fill" style={{ width: `${percent}%` }} />
        </div>
      )}
      <div className="quota-panel__meta">
        {status === "unlimited" ? (
          <span>{formatUsd(cost_usd)} today</span>
        ) : (
          <span>
            {formatUsd(cost_usd)} / {formatUsd(dailyLimitUsd)}
          </span>
        )}
        <span>{runs} runs</span>
      </div>
      {status === "exceeded" && (
        <div className="quota-panel__hint">
          <span>{quotaUi.helperText}</span>
          <Link to="/settings">Add provider key</Link>
        </div>
      )}
      {status === "warning" && (
        <div className="quota-panel__hint">
          <span>{formatUsd(limit.remaining_usd)} remaining today.</span>
          <Link to="/settings">Use your own key</Link>
        </div>
      )}
    </div>
  );
}
