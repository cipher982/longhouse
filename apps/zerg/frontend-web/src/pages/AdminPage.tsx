import React, { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { useAuth } from "../lib/auth";
import config from "../lib/config";

// Types for admin user usage data
interface UserPeriodUsage {
  tokens: number;
  cost_usd: number;
  runs: number;
}

interface AdminUserUsage {
  today: UserPeriodUsage;
  seven_days: UserPeriodUsage;
  thirty_days: UserPeriodUsage;
}

interface AdminUserRow {
  id: number;
  email: string;
  display_name: string | null;
  role: string;
  is_active: boolean;
  created_at: string | null;
  usage: AdminUserUsage;
}

interface AdminUsersResponse {
  users: AdminUserRow[];
  total: number;
  limit: number;
  offset: number;
}

interface DailyBreakdown {
  date: string;
  tokens: number;
  cost_usd: number;
  runs: number;
}

interface TopAgentUsage {
  agent_id: number;
  name: string;
  tokens: number;
  cost_usd: number;
  runs: number;
}

interface AdminUserDetailResponse {
  user: AdminUserRow;
  period: string;
  summary: UserPeriodUsage;
  daily_breakdown: DailyBreakdown[];
  top_agents: TopAgentUsage[];
}

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

// API functions (top agents are included in summary)
async function fetchOpsSummary(): Promise<OpsSummary> {
  const response = await fetch(`${config.apiBaseUrl}/ops/summary`, {
    credentials: 'include', // Cookie auth
  });

  if (!response.ok) {
    if (response.status === 403) {
      throw new Error("Admin access required");
    }
    throw new Error("Failed to fetch ops summary");
  }

  return response.json();
}

// Database management types and functions
interface DatabaseResetRequest {
  confirmation_password?: string;
  reset_type: "clear_data" | "full_rebuild";
}

interface SuperAdminStatusResponse {
  is_super_admin: boolean;
  requires_password: boolean;
}

async function fetchSuperAdminStatus(): Promise<SuperAdminStatusResponse> {
  const response = await fetch(`${config.apiBaseUrl}/admin/super-admin-status`, {
    credentials: 'include', // Cookie auth
  });

  if (!response.ok) {
    throw new Error("Failed to fetch super admin status");
  }

  return response.json();
}

async function resetDatabase(request: DatabaseResetRequest): Promise<any> {
  const response = await fetch(`${config.apiBaseUrl}/admin/reset-database`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    credentials: 'include', // Cookie auth
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || "Failed to reset database");
  }

  return response.json();
}

// Admin users API functions
async function fetchAdminUsers(
  sort: string = "cost_today",
  order: string = "desc"
): Promise<AdminUsersResponse> {
  const response = await fetch(
    `${config.apiBaseUrl}/admin/users?sort=${sort}&order=${order}&limit=100`,
    { credentials: "include" }
  );

  if (!response.ok) {
    throw new Error("Failed to fetch users");
  }

  return response.json();
}

async function fetchUserDetail(
  userId: number,
  period: string = "7d"
): Promise<AdminUserDetailResponse> {
  const response = await fetch(
    `${config.apiBaseUrl}/admin/users/${userId}/usage?period=${period}`,
    { credentials: "include" }
  );

  if (!response.ok) {
    throw new Error("Failed to fetch user details");
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

// Confirmation Modal component
function ConfirmationModal({
  isOpen,
  onClose,
  onConfirm,
  title,
  message,
  confirmText = "Confirm",
  isDangerous = false,
  requirePassword = false,
}: {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: (password?: string) => void;
  title: string;
  message: string;
  confirmText?: string;
  isDangerous?: boolean;
  requirePassword?: boolean;
}) {
  const [password, setPassword] = useState("");

  if (!isOpen) return null;

  const handleConfirm = () => {
    onConfirm(requirePassword ? password : undefined);
    setPassword("");
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        <p>{message}</p>
        {requirePassword && (
          <div className="form-group" style={{ marginTop: "16px" }}>
            <input
              type="password"
              className="form-input"
              placeholder="Confirmation password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
        )}
        <div className="modal-actions">
          <button className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            className={isDangerous ? "btn-danger" : "btn-primary"}
            onClick={handleConfirm}
            disabled={requirePassword && !password}
          >
            {confirmText}
          </button>
        </div>
      </div>
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

// Format cost helper
function formatCost(cost: number): string {
  if (cost >= 1) return `$${cost.toFixed(2)}`;
  if (cost >= 0.01) return `$${cost.toFixed(3)}`;
  return `$${cost.toFixed(4)}`;
}

// Users table component with usage stats
function UsersTable({
  users,
  sortField,
  sortOrder,
  onSort,
  onUserClick,
}: {
  users: AdminUserRow[];
  sortField: string;
  sortOrder: string;
  onSort: (field: string) => void;
  onUserClick: (userId: number) => void;
}) {
  const renderSortArrow = (field: string) => {
    if (sortField !== field) return null;
    return <span className="sort-arrow">{sortOrder === "asc" ? "▲" : "▼"}</span>;
  };

  if (users.length === 0) {
    return (
      <div className="empty-state">
        <p>No users found</p>
      </div>
    );
  }

  return (
    <div className="users-table-container">
      <table className="users-table">
        <thead>
          <tr>
            <th onClick={() => onSort("email")} className="sortable">
              User {renderSortArrow("email")}
            </th>
            <th>Role</th>
            <th onClick={() => onSort("cost_today")} className="sortable numeric">
              Today {renderSortArrow("cost_today")}
            </th>
            <th onClick={() => onSort("cost_7d")} className="sortable numeric">
              7 Days {renderSortArrow("cost_7d")}
            </th>
            <th onClick={() => onSort("cost_30d")} className="sortable numeric">
              30 Days {renderSortArrow("cost_30d")}
            </th>
            <th onClick={() => onSort("created_at")} className="sortable">
              Joined {renderSortArrow("created_at")}
            </th>
          </tr>
        </thead>
        <tbody>
          {users.map((user) => (
            <tr
              key={user.id}
              onClick={() => onUserClick(user.id)}
              className="clickable-row"
            >
              <td className="user-cell">
                <div className="user-info">
                  <span className="user-email">{user.email}</span>
                  {user.display_name && (
                    <span className="user-display-name">{user.display_name}</span>
                  )}
                </div>
              </td>
              <td>
                <span className={`role-badge role-${user.role.toLowerCase()}`}>
                  {user.role}
                </span>
              </td>
              <td className="numeric">{formatCost(user.usage.today.cost_usd)}</td>
              <td className="numeric">{formatCost(user.usage.seven_days.cost_usd)}</td>
              <td className="numeric">{formatCost(user.usage.thirty_days.cost_usd)}</td>
              <td>
                {user.created_at
                  ? new Date(user.created_at).toLocaleDateString()
                  : "-"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// User detail modal
function UserDetailModal({
  userId,
  isOpen,
  onClose,
}: {
  userId: number | null;
  isOpen: boolean;
  onClose: () => void;
}) {
  const [period, setPeriod] = useState<"today" | "7d" | "30d">("7d");

  const { data: detail, isLoading } = useQuery({
    queryKey: ["admin-user-detail", userId, period],
    queryFn: () => fetchUserDetail(userId!, period),
    enabled: isOpen && userId !== null,
  });

  if (!isOpen || userId === null) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content user-detail-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>User Usage Details</h3>
          <button className="modal-close" onClick={onClose}>×</button>
        </div>

        {isLoading ? (
          <div className="loading-state">Loading user details...</div>
        ) : detail ? (
          <div className="user-detail-content">
            {/* User Info */}
            <div className="user-detail-header">
              <div className="user-detail-info">
                <h4>{detail.user.email}</h4>
                {detail.user.display_name && (
                  <p className="display-name">{detail.user.display_name}</p>
                )}
                <span className={`role-badge role-${detail.user.role.toLowerCase()}`}>
                  {detail.user.role}
                </span>
              </div>
              <div className="period-selector">
                <select
                  value={period}
                  onChange={(e) => setPeriod(e.target.value as "today" | "7d" | "30d")}
                >
                  <option value="today">Today</option>
                  <option value="7d">Last 7 Days</option>
                  <option value="30d">Last 30 Days</option>
                </select>
              </div>
            </div>

            {/* Usage Summary Cards */}
            <div className="usage-summary-row">
              <div className="usage-summary-card">
                <span className="label">Tokens</span>
                <span className="value">{detail.summary.tokens.toLocaleString()}</span>
              </div>
              <div className="usage-summary-card">
                <span className="label">Cost</span>
                <span className="value">{formatCost(detail.summary.cost_usd)}</span>
              </div>
              <div className="usage-summary-card">
                <span className="label">Runs</span>
                <span className="value">{detail.summary.runs}</span>
              </div>
            </div>

            {/* Daily Breakdown */}
            {detail.daily_breakdown.length > 0 && (
              <div className="detail-section">
                <h5>Daily Breakdown</h5>
                <table className="breakdown-table">
                  <thead>
                    <tr>
                      <th>Date</th>
                      <th className="numeric">Tokens</th>
                      <th className="numeric">Cost</th>
                      <th className="numeric">Runs</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.daily_breakdown.map((day) => (
                      <tr key={day.date}>
                        <td>{day.date}</td>
                        <td className="numeric">{day.tokens.toLocaleString()}</td>
                        <td className="numeric">{formatCost(day.cost_usd)}</td>
                        <td className="numeric">{day.runs}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Top Agents */}
            {detail.top_agents.length > 0 && (
              <div className="detail-section">
                <h5>Top Agents by Cost</h5>
                <table className="breakdown-table">
                  <thead>
                    <tr>
                      <th>Agent</th>
                      <th className="numeric">Tokens</th>
                      <th className="numeric">Cost</th>
                      <th className="numeric">Runs</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.top_agents.map((agent) => (
                      <tr key={agent.agent_id}>
                        <td>{agent.name}</td>
                        <td className="numeric">{agent.tokens.toLocaleString()}</td>
                        <td className="numeric">{formatCost(agent.cost_usd)}</td>
                        <td className="numeric">{agent.runs}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ) : (
          <div className="error-state">Failed to load user details</div>
        )}
      </div>
    </div>
  );
}

function AdminPage() {
  const { user } = useAuth();
  const [selectedWindow, setSelectedWindow] = useState<"today" | "7d" | "30d">("today");
  const [modalState, setModalState] = useState<{
    isOpen: boolean;
    type: "clear_data" | "full_rebuild" | null;
    requirePassword: boolean;
  }>({
    isOpen: false,
    type: null,
    requirePassword: false,
  });

  // Users table state
  const [usersSortField, setUsersSortField] = useState("cost_today");
  const [usersSortOrder, setUsersSortOrder] = useState("desc");
  const [selectedUserId, setSelectedUserId] = useState<number | null>(null);
  const [userDetailOpen, setUserDetailOpen] = useState(false);

  // Ops summary query - FIXED: Move ALL hooks before any conditional logic
  const { data: summary, isLoading: summaryLoading, error: summaryError } = useQuery({
    queryKey: ["ops-summary"],
    queryFn: fetchOpsSummary,
    refetchInterval: 30000, // Refresh every 30 seconds
    enabled: !!user, // Only run query when user is available
  });

  // Super admin status query
  const { data: adminStatus } = useQuery({
    queryKey: ["super-admin-status"],
    queryFn: fetchSuperAdminStatus,
    enabled: !!user,
  });

  // Admin users query
  const { data: usersData, isLoading: usersLoading } = useQuery({
    queryKey: ["admin-users", usersSortField, usersSortOrder],
    queryFn: () => fetchAdminUsers(usersSortField, usersSortOrder),
    enabled: !!user,
    refetchInterval: 60000, // Refresh every minute
  });

  // Database reset mutation
  const resetMutation = useMutation({
    mutationFn: resetDatabase,
    onSuccess: (data) => {
      toast.success(data.message || "Database operation completed successfully");
      setModalState({ isOpen: false, type: null, requirePassword: false });
    },
    onError: (error: Error) => {
      toast.error(error.message || "Database operation failed");
    },
  });

  // Handle permission errors - FIXED: Move ALL hooks before conditional logic
  React.useEffect(() => {
    if (summaryError instanceof Error && summaryError.message.includes("Admin access required")) {
      toast.error("Admin access required to view this page");
    }
  }, [summaryError]);

  // Handle users table sort
  const handleUsersSort = (field: string) => {
    if (field === usersSortField) {
      setUsersSortOrder(usersSortOrder === "asc" ? "desc" : "asc");
    } else {
      setUsersSortField(field);
      setUsersSortOrder("desc");
    }
  };

  // Handle user row click
  const handleUserClick = (userId: number) => {
    setSelectedUserId(userId);
    setUserDetailOpen(true);
  };

  // Check if user is admin (this should be checked by the router, but let's be safe)
  if (!user) {
    return <div>Loading...</div>;
  }

  const formatCurrency = (value: number) => `$${value.toFixed(4)}`;
  const formatPercent = (value: number) => `${value.toFixed(1)}%`;

  // Admin action handlers
  const handleClearData = () => {
    setModalState({
      isOpen: true,
      type: "clear_data",
      requirePassword: adminStatus?.requires_password ?? false,
    });
  };

  const handleFullReset = () => {
    setModalState({
      isOpen: true,
      type: "full_rebuild",
      requirePassword: adminStatus?.requires_password ?? false,
    });
  };

  const handleConfirmReset = (password?: string) => {
    if (!modalState.type) return;

    resetMutation.mutate({
      reset_type: modalState.type,
      confirmation_password: password,
    });
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

          {/* Users Usage Section */}
          <div className="admin-section">
            <h3>User LLM Usage</h3>
            <p className="section-description">
              Click on a user to see detailed usage breakdown
            </p>
            {usersLoading ? (
              <div className="loading-state">Loading users...</div>
            ) : usersData?.users ? (
              <UsersTable
                users={usersData.users}
                sortField={usersSortField}
                sortOrder={usersSortOrder}
                onSort={handleUsersSort}
                onUserClick={handleUserClick}
              />
            ) : (
              <div className="empty-state">No users found</div>
            )}
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

          {/* Admin Actions */}
          <div className="admin-section">
            <h3>Database Management</h3>
            <div className="admin-actions">
              <div className="action-group">
                <button
                  className="btn-warning"
                  onClick={handleClearData}
                  disabled={resetMutation.isPending}
                >
                  Clear User Data
                </button>
                <p className="action-description">
                  Remove all user-generated data (agents, runs, workflows) while preserving user accounts
                </p>
              </div>
              <div className="action-group">
                <button
                  className="btn-danger"
                  onClick={handleFullReset}
                  disabled={resetMutation.isPending}
                >
                  Full Database Reset
                </button>
                <p className="action-description">
                  Drop and recreate all tables (destructive operation)
                </p>
              </div>
            </div>
          </div>
        </>
      ) : null}

      {/* Confirmation Modal */}
      <ConfirmationModal
        isOpen={modalState.isOpen}
        onClose={() => setModalState({ isOpen: false, type: null, requirePassword: false })}
        onConfirm={handleConfirmReset}
        title={
          modalState.type === "clear_data"
            ? "Clear User Data"
            : "Full Database Reset"
        }
        message={
          modalState.type === "clear_data"
            ? "This will remove all user-generated data (agents, runs, workflows) but preserve user accounts. This action cannot be undone."
            : "This will drop and recreate all database tables. All data will be lost. This action cannot be undone."
        }
        confirmText={resetMutation.isPending ? "Processing..." : "Confirm"}
        isDangerous={true}
        requirePassword={modalState.requirePassword}
      />

      {/* User Detail Modal */}
      <UserDetailModal
        userId={selectedUserId}
        isOpen={userDetailOpen}
        onClose={() => {
          setUserDetailOpen(false);
          setSelectedUserId(null);
        }}
      />
    </div>
  );
}

export default AdminPage;
