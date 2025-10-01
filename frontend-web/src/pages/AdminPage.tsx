import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { useAuth } from "../lib/auth";

// Types for ops data - matching actual backend contract
interface OpsSummary {
  runs_today: number;
  cost_today_usd: number | null;
  budget_user: {
    limit_cents: number;
    used_usd: number;
    percent: number | null;
  };
  budget_global: {
    limit_cents: number;
    used_usd: number;
    percent: number | null;
  };
  active_users_24h: number;
  agents_total: number;
  agents_scheduled: number;
  latency_ms: {
    p50: number;
    p95: number;
  };
  errors_last_hour: number;
  top_agents_today: OpsTopAgent[];
}

interface OpsSeriesPoint {
  hour_iso: string; // Matches backend service field name
  value: number;
}

interface OpsTopAgent {
  agent_id: number;
  name: string;
  owner_email: string;
  runs: number;
  cost_usd: number | null;
  p95_ms: number;
}

// TimeSeriesResponse interface removed - not currently used

// API functions (top agents are included in summary)
async function fetchOpsSummary(): Promise<OpsSummary> {
  const token = localStorage.getItem("zerg_jwt");
  if (!token) {
    throw new Error("No auth token");
  }

  const response = await fetch("/api/ops/summary", {
    headers: { "Authorization": `Bearer ${token}` },
  });

  if (!response.ok) {
    if (response.status === 403) {
      throw new Error("Admin access required");
    }
    throw new Error("Failed to fetch ops summary");
  }

  return response.json();
}

// fetchTimeSeries removed - not currently used

// fetchTopAgents removed - top agents are included in ops summary

// Metric card component
function MetricCard({
  title,
  value,
  subtitle,
  color = "#10b981"
}: {
  title: string;
  value: string | number;
  subtitle?: string;
  color?: string;
}) {
  return (
    <div className="metric-card">
      <div className="metric-header">
        <h4 style={{ color }}>{title}</h4>
      </div>
      <div className="metric-value">{value}</div>
      {subtitle && <div className="metric-subtitle">{subtitle}</div>}
    </div>
  );
}

// Top agents table component - using real backend contract
function TopAgentsTable({ agents }: { agents: OpsTopAgent[] }) {
  if (agents.length === 0) {
    return (
      <div className="empty-state">
        <p>No agent data available</p>
      </div>
    );
  }

  return (
    <div className="top-agents-table">
      <table>
        <thead>
          <tr>
            <th>Agent Name</th>
            <th>Owner</th>
            <th>Runs</th>
            <th>Cost (USD)</th>
            <th>P95 Latency</th>
          </tr>
        </thead>
        <tbody>
          {agents.map((agent) => (
            <tr key={agent.agent_id}>
              <td className="agent-name">{agent.name}</td>
              <td className="owner-email">{agent.owner_email}</td>
              <td className="runs-count">{agent.runs}</td>
              <td className="cost">
                {agent.cost_usd !== null ? `$${agent.cost_usd.toFixed(4)}` : 'N/A'}
              </td>
              <td className="latency">
                {agent.p95_ms}ms
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AdminPage() {
  const { user } = useAuth();
  const [selectedWindow, setSelectedWindow] = useState<"today" | "7d" | "30d">("today");

  // Ops summary query - FIXED: Move ALL hooks before any conditional logic
  const { data: summary, isLoading: summaryLoading, error: summaryError } = useQuery({
    queryKey: ["ops-summary"],
    queryFn: fetchOpsSummary,
    refetchInterval: 30000, // Refresh every 30 seconds
    enabled: !!user, // Only run query when user is available
  });

  // Handle permission errors - FIXED: Move ALL hooks before conditional logic
  React.useEffect(() => {
    if (summaryError instanceof Error && summaryError.message.includes("Admin access required")) {
      toast.error("Admin access required to view this page");
    }
  }, [summaryError]);

  // Check if user is admin (this should be checked by the router, but let's be safe)
  if (!user) {
    return <div>Loading...</div>;
  }

  const formatCurrency = (value: number) => `$${value.toFixed(4)}`;
  const formatPercent = (value: number) => `${value.toFixed(1)}%`;

  return (
    <div className="admin-page">
      <div className="admin-header">
        <h1>Operations Dashboard</h1>
        <div className="window-selector">
          <label>Time Window:</label>
          <select
            value={selectedWindow}
            onChange={(e) => setSelectedWindow(e.target.value as "today" | "7d" | "30d")}
          >
            <option value="today">Today</option>
            <option value="7d">Last 7 Days</option>
            <option value="30d">Last 30 Days</option>
          </select>
        </div>
      </div>

      {summaryLoading ? (
        <div className="loading-state">Loading operations data...</div>
      ) : summaryError ? (
        <div className="error-state">
          <p>Failed to load operations data</p>
          <button onClick={() => window.location.reload()}>Retry</button>
        </div>
      ) : summary ? (
        <>
          {/* Key Metrics - using real backend data */}
          <div className="metrics-grid">
            <MetricCard
              title="Runs Today"
              value={summary.runs_today}
              subtitle="Total executions"
              color="#3b82f6"
            />
            <MetricCard
              title="Errors (1h)"
              value={summary.errors_last_hour}
              subtitle="Failed runs"
              color="#ef4444"
            />
            <MetricCard
              title="Cost Today"
              value={summary.cost_today_usd !== null ? formatCurrency(summary.cost_today_usd) : "N/A"}
              subtitle="USD spent"
              color="#10b981"
            />
            <MetricCard
              title="User Budget"
              value={
                summary.budget_user.percent !== null
                  ? formatPercent(summary.budget_user.percent)
                  : "No limit"
              }
              subtitle={
                summary.budget_user.limit_cents > 0
                  ? `of $${(summary.budget_user.limit_cents / 100).toFixed(2)}`
                  : "Unlimited"
              }
              color={
                summary.budget_user.percent === null ? "#6b7280" :
                summary.budget_user.percent > 80 ? "#ef4444" :
                summary.budget_user.percent > 60 ? "#f59e0b" : "#10b981"
              }
            />
            <MetricCard
              title="Global Budget"
              value={
                summary.budget_global.percent !== null
                  ? formatPercent(summary.budget_global.percent)
                  : "No limit"
              }
              subtitle={
                summary.budget_global.limit_cents > 0
                  ? `of $${(summary.budget_global.limit_cents / 100).toFixed(2)}`
                  : "Unlimited"
              }
              color={
                summary.budget_global.percent === null ? "#6b7280" :
                summary.budget_global.percent > 80 ? "#ef4444" :
                summary.budget_global.percent > 60 ? "#f59e0b" : "#10b981"
              }
            />
            <MetricCard
              title="Latency P95"
              value={`${summary.latency_ms.p95}ms`}
              subtitle={`P50: ${summary.latency_ms.p50}ms`}
              color="#8b5cf6"
            />
          </div>

          {/* Top Agents Section - using data from summary */}
          <div className="admin-section">
            <h3>Top Performing Agents (Today)</h3>
            <TopAgentsTable agents={summary.top_agents_today} />
          </div>

          {/* System Information - using real backend data */}
          <div className="admin-section">
            <h3>System Information</h3>
            <div className="system-info">
              <div className="info-grid">
                <div className="info-item">
                  <span className="info-label">Total Agents:</span>
                  <span className="info-value">{summary.agents_total}</span>
                </div>
                <div className="info-item">
                  <span className="info-label">Scheduled Agents:</span>
                  <span className="info-value">{summary.agents_scheduled}</span>
                </div>
                <div className="info-item">
                  <span className="info-label">Active Users (24h):</span>
                  <span className="info-value">{summary.active_users_24h}</span>
                </div>
                <div className="info-item">
                  <span className="info-label">User Budget Used:</span>
                  <span className="info-value">
                    ${summary.budget_user.used_usd.toFixed(4)}
                    {summary.budget_user.limit_cents > 0 && (
                      <span> / ${(summary.budget_user.limit_cents / 100).toFixed(2)}</span>
                    )}
                  </span>
                </div>
                <div className="info-item">
                  <span className="info-label">Global Budget Used:</span>
                  <span className="info-value">
                    ${summary.budget_global.used_usd.toFixed(4)}
                    {summary.budget_global.limit_cents > 0 && (
                      <span> / ${(summary.budget_global.limit_cents / 100).toFixed(2)}</span>
                    )}
                  </span>
                </div>
                <div className="info-item">
                  <span className="info-label">Median Latency:</span>
                  <span className="info-value">{summary.latency_ms.p50}ms</span>
                </div>
              </div>
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}

export default AdminPage;