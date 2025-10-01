import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { useAuth } from "../lib/auth";

// Types for ops data
interface OpsSummary {
  runs_today: number;
  errors_today: number;
  cost_today: number;
  runs_7d: number;
  errors_7d: number;
  cost_7d: number;
  runs_30d: number;
  errors_30d: number;
  cost_30d: number;
  budget_used: number; // percentage 0-100
  budget_limit: number;
}

interface OpsSeriesPoint {
  timestamp: string;
  value: number;
}

interface OpsTopAgent {
  agent_id: number;
  agent_name: string;
  total_runs: number;
  success_rate: number; // percentage 0-100
  avg_cost: number;
}

interface TimeSeriesResponse {
  series: OpsSeriesPoint[];
}

interface TopAgentsResponse {
  top_agents: OpsTopAgent[];
}

// API functions
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

async function fetchTimeSeries(metric: string, window: string = "today"): Promise<TimeSeriesResponse> {
  const token = localStorage.getItem("zerg_jwt");
  if (!token) {
    throw new Error("No auth token");
  }

  const response = await fetch(`/api/ops/timeseries?metric=${metric}&window=${window}`, {
    headers: { "Authorization": `Bearer ${token}` },
  });

  if (!response.ok) {
    throw new Error("Failed to fetch time series data");
  }

  return response.json();
}

async function fetchTopAgents(window: string = "today", limit: number = 5): Promise<TopAgentsResponse> {
  const token = localStorage.getItem("zerg_jwt");
  if (!token) {
    throw new Error("No auth token");
  }

  const response = await fetch(`/api/ops/top?kind=agents&window=${window}&limit=${limit}`, {
    headers: { "Authorization": `Bearer ${token}` },
  });

  if (!response.ok) {
    throw new Error("Failed to fetch top agents");
  }

  return response.json();
}

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

// Top agents table component
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
            <th>Runs</th>
            <th>Success Rate</th>
            <th>Avg Cost</th>
          </tr>
        </thead>
        <tbody>
          {agents.map((agent) => (
            <tr key={agent.agent_id}>
              <td className="agent-name">{agent.agent_name}</td>
              <td className="runs-count">{agent.total_runs}</td>
              <td className="success-rate">
                <span className={`rate rate--${agent.success_rate >= 90 ? 'good' : agent.success_rate >= 70 ? 'ok' : 'poor'}`}>
                  {agent.success_rate.toFixed(1)}%
                </span>
              </td>
              <td className="avg-cost">${agent.avg_cost.toFixed(4)}</td>
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

  // Check if user is admin (this should be checked by the router, but let's be safe)
  if (!user) {
    return <div>Loading...</div>;
  }

  // Ops summary query
  const { data: summary, isLoading: summaryLoading, error: summaryError } = useQuery({
    queryKey: ["ops-summary"],
    queryFn: fetchOpsSummary,
    refetchInterval: 30000, // Refresh every 30 seconds
  });

  // Top agents query
  const { data: topAgents, isLoading: agentsLoading } = useQuery({
    queryKey: ["top-agents", selectedWindow],
    queryFn: () => fetchTopAgents(selectedWindow, 10),
    refetchInterval: 60000, // Refresh every minute
  });

  // Handle permission errors
  React.useEffect(() => {
    if (summaryError instanceof Error && summaryError.message.includes("Admin access required")) {
      toast.error("Admin access required to view this page");
    }
  }, [summaryError]);

  const formatCurrency = (value: number) => `$${value.toFixed(4)}`;
  const formatPercent = (value: number) => `${value.toFixed(1)}%`;

  const getWindowLabel = (window: string) => {
    switch (window) {
      case "today": return "Today";
      case "7d": return "7 Days";
      case "30d": return "30 Days";
      default: return window;
    }
  };

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
          {/* Key Metrics */}
          <div className="metrics-grid">
            <MetricCard
              title="Runs"
              value={
                selectedWindow === "today" ? summary.runs_today :
                selectedWindow === "7d" ? summary.runs_7d :
                summary.runs_30d
              }
              subtitle={getWindowLabel(selectedWindow)}
              color="#3b82f6"
            />
            <MetricCard
              title="Errors"
              value={
                selectedWindow === "today" ? summary.errors_today :
                selectedWindow === "7d" ? summary.errors_7d :
                summary.errors_30d
              }
              subtitle={getWindowLabel(selectedWindow)}
              color="#ef4444"
            />
            <MetricCard
              title="Cost"
              value={formatCurrency(
                selectedWindow === "today" ? summary.cost_today :
                selectedWindow === "7d" ? summary.cost_7d :
                summary.cost_30d
              )}
              subtitle={getWindowLabel(selectedWindow)}
              color="#10b981"
            />
            <MetricCard
              title="Budget Used"
              value={formatPercent(summary.budget_used)}
              subtitle={`of ${formatCurrency(summary.budget_limit)}`}
              color={summary.budget_used > 80 ? "#ef4444" : summary.budget_used > 60 ? "#f59e0b" : "#10b981"}
            />
          </div>

          {/* Top Agents Section */}
          <div className="admin-section">
            <h3>Top Performing Agents ({getWindowLabel(selectedWindow)})</h3>
            {agentsLoading ? (
              <div>Loading top agents...</div>
            ) : topAgents?.top_agents ? (
              <TopAgentsTable agents={topAgents.top_agents} />
            ) : (
              <div className="empty-state">No agent data available</div>
            )}
          </div>

          {/* System Information */}
          <div className="admin-section">
            <h3>System Information</h3>
            <div className="system-info">
              <div className="info-grid">
                <div className="info-item">
                  <span className="info-label">Total Budget:</span>
                  <span className="info-value">{formatCurrency(summary.budget_limit)}</span>
                </div>
                <div className="info-item">
                  <span className="info-label">Budget Remaining:</span>
                  <span className="info-value">
                    {formatCurrency(summary.budget_limit * (1 - summary.budget_used / 100))}
                  </span>
                </div>
                <div className="info-item">
                  <span className="info-label">Success Rate (30d):</span>
                  <span className="info-value">
                    {summary.runs_30d > 0 ? formatPercent((1 - summary.errors_30d / summary.runs_30d) * 100) : "N/A"}
                  </span>
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