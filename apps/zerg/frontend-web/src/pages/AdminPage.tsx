import React, { useMemo, useState, useEffect } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { useAuth } from "../lib/auth";
import config from "../lib/config";
import {
  Button,
  Card,
  SectionHeader,
  EmptyState,
  Table,
  Badge,
  PageShell,
  Spinner
} from "../components/ui";

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
  is_demo?: boolean;
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

async function resetDatabase(request: DatabaseResetRequest): Promise<{ message: string }> {
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

interface DemoUserCreateRequest {
  email?: string;
  display_name?: string;
}

interface DemoUserResponse {
  id: number;
  email: string;
  display_name: string | null;
  is_demo: boolean;
}

async function createDemoUser(request: DemoUserCreateRequest): Promise<DemoUserResponse> {
  const response = await fetch(`${config.apiBaseUrl}/admin/demo-users`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || "Failed to create demo user");
  }

  return response.json();
}

async function seedScenarioForUser(request: { name: string; owner_email: string; clean: boolean }): Promise<void> {
  const response = await fetch(`${config.apiBaseUrl}/admin/seed-scenario`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || "Failed to seed scenario");
  }
}

interface DemoResetResponse {
  user_id: number;
  email: string;
  cleared: Record<string, number>;
}

async function resetDemoUser(userId: number): Promise<DemoResetResponse> {
  const response = await fetch(`${config.apiBaseUrl}/admin/demo-users/${userId}/reset`, {
    method: "POST",
    credentials: "include",
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || "Failed to reset demo account");
  }

  return response.json();
}

async function impersonateUser(request: { user_id: number }): Promise<void> {
  const response = await fetch(`${config.apiBaseUrl}/auth/impersonate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || "Failed to impersonate user");
  }
}

// Metric card component
function MetricCard({
  title,
  value,
  subtitle,
  color = "var(--color-intent-success)"
}: {
  title: string;
  value: string | number;
  subtitle?: string;
  color?: string;
}) {
  return (
    <Card className="metric-card" style={{ "--metric-accent": color } as React.CSSProperties}>
      <Card.Header>
        <h4 className="metric-title">{title}</h4>
      </Card.Header>
      <Card.Body>
        <div className="metric-value">{value}</div>
        {subtitle && <div className="metric-subtitle">{subtitle}</div>}
      </Card.Body>
    </Card>
  );
}

// Confirmation Modal component with React Portal
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

  // Lock body scroll when modal is open
  useEffect(() => {
    if (isOpen) {
      const originalOverflow = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
      return () => {
        document.body.style.overflow = originalOverflow;
      };
    }
  }, [isOpen]);

  // Handle Escape key
  useEffect(() => {
    if (!isOpen) return;

    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };

    window.addEventListener('keydown', handleEscape);
    return () => window.removeEventListener('keydown', handleEscape);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const handleConfirm = () => {
    onConfirm(requirePassword ? password : undefined);
    setPassword("");
  };

  const handleBackdropClick = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) {
      onClose();
    }
  };

  const modalContent = (
    <div
      className="admin-confirm-overlay"
      onClick={handleBackdropClick}
    >
      <Card
        className="admin-confirm-card"
        onClick={(e: React.MouseEvent) => e.stopPropagation()}
      >
        <Card.Header>
          <h3 className="admin-confirm-title">{title}</h3>
        </Card.Header>
        <Card.Body>
          <p>{message}</p>
          {requirePassword && (
            <div className="form-group admin-confirm-field">
              <input
                type="password"
                className="ui-input"
                placeholder="Confirmation password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && password) {
                    handleConfirm();
                  }
                }}
                autoFocus
              />
            </div>
          )}
          <div className="modal-actions admin-confirm-actions">
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button
              variant={isDangerous ? "danger" : "primary"}
              onClick={handleConfirm}
              disabled={requirePassword && !password}
            >
              {confirmText}
            </Button>
          </div>
        </Card.Body>
      </Card>
    </div>
  );

  // Render modal at document.body level using Portal
  return createPortal(modalContent, document.body);
}

// Top agents table component - using real backend contract
function TopAgentsTable({ agents }: { agents: OpsTopAgent[] }) {
  if (agents.length === 0) {
    return (
      <EmptyState
        title="No agent data available"
        description="Data will appear once agents start running."
      />
    );
  }

  return (
    <Table>
      <Table.Header>
        <Table.Cell isHeader>Agent Name</Table.Cell>
        <Table.Cell isHeader>Owner</Table.Cell>
        <Table.Cell isHeader>Runs</Table.Cell>
        <Table.Cell isHeader>Cost (USD)</Table.Cell>
        <Table.Cell isHeader>P95 Latency</Table.Cell>
      </Table.Header>
      <Table.Body>
        {agents.map((agent) => (
          <Table.Row key={agent.agent_id}>
            <Table.Cell className="agent-name">{agent.name}</Table.Cell>
            <Table.Cell className="owner-email">{agent.owner_email}</Table.Cell>
            <Table.Cell className="runs-count">{agent.runs}</Table.Cell>
            <Table.Cell className="cost">
              {agent.cost_usd !== null ? `$${agent.cost_usd.toFixed(4)}` : 'N/A'}
            </Table.Cell>
            <Table.Cell className="latency">
              {agent.p95_ms}ms
            </Table.Cell>
          </Table.Row>
        ))}
      </Table.Body>
    </Table>
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
      <EmptyState
        title="No users found"
        description="Try a different sort or check back later."
      />
    );
  }

  return (
    <Table className="users-table">
      <Table.Header>
        <Table.Cell isHeader onClick={() => onSort("email")} className="admin-table-header">
          User {renderSortArrow("email")}
        </Table.Cell>
        <Table.Cell isHeader>Role</Table.Cell>
        <Table.Cell isHeader onClick={() => onSort("cost_today")} className="admin-table-header admin-table-header--numeric">
          Today {renderSortArrow("cost_today")}
        </Table.Cell>
        <Table.Cell isHeader onClick={() => onSort("cost_7d")} className="admin-table-header admin-table-header--numeric">
          7 Days {renderSortArrow("cost_7d")}
        </Table.Cell>
        <Table.Cell isHeader onClick={() => onSort("cost_30d")} className="admin-table-header admin-table-header--numeric">
          30 Days {renderSortArrow("cost_30d")}
        </Table.Cell>
        <Table.Cell isHeader onClick={() => onSort("created_at")} className="admin-table-header">
          Joined {renderSortArrow("created_at")}
        </Table.Cell>
      </Table.Header>
      <Table.Body>
        {users.map((user) => (
          <Table.Row
            key={user.id}
            onClick={() => onUserClick(user.id)}
            className="clickable-row"
          >
            <Table.Cell className="user-cell">
              <div className="user-info">
                <span className="user-email">{user.email}</span>
                {user.display_name && (
                  <span className="user-display-name">{user.display_name}</span>
                )}
                {user.is_demo && (
                  <Badge variant="warning" className="user-demo-badge">Demo</Badge>
                )}
              </div>
            </Table.Cell>
            <Table.Cell>
              <Badge variant={user.role === 'admin' ? 'success' : 'neutral'}>
                {user.role}
              </Badge>
            </Table.Cell>
            <Table.Cell className="admin-table-cell--numeric">{formatCost(user.usage.today.cost_usd)}</Table.Cell>
            <Table.Cell className="admin-table-cell--numeric">{formatCost(user.usage.seven_days.cost_usd)}</Table.Cell>
            <Table.Cell className="admin-table-cell--numeric">{formatCost(user.usage.thirty_days.cost_usd)}</Table.Cell>
            <Table.Cell>
              {user.created_at
                ? new Date(user.created_at).toLocaleDateString()
                : "-"}
            </Table.Cell>
          </Table.Row>
        ))}
      </Table.Body>
    </Table>
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
  const queryClient = useQueryClient();
  const metricColors = {
    success: "var(--color-intent-success)",
    warning: "var(--color-intent-warning)",
    error: "var(--color-intent-error)",
    primary: "var(--color-brand-primary)",
    accent: "var(--color-neon-secondary)",
    muted: "var(--color-text-muted)",
  };
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
  const [demoEmail, setDemoEmail] = useState("");
  const [demoDisplayName, setDemoDisplayName] = useState("");
  const [demoScenario, setDemoScenario] = useState("swarm-mvp");
  const [selectedDemoUserId, setSelectedDemoUserId] = useState<number | null>(null);
  const [demoResetState, setDemoResetState] = useState<{
    isOpen: boolean;
    target: AdminUserRow | null;
  }>({
    isOpen: false,
    target: null,
  });

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

  const demoUsers = useMemo(() => {
    return (usersData?.users ?? []).filter((demoUser) => demoUser.is_demo);
  }, [usersData]);

  useEffect(() => {
    if (!selectedDemoUserId && demoUsers.length > 0) {
      setSelectedDemoUserId(demoUsers[0].id);
    }
  }, [demoUsers, selectedDemoUserId]);

  useEffect(() => {
    if (summaryLoading || usersLoading) {
      document.body.removeAttribute("data-ready");
      return;
    }

    document.body.setAttribute("data-ready", "true");

    return () => {
      document.body.removeAttribute("data-ready");
    };
  }, [summaryLoading, usersLoading]);

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

  const createDemoMutation = useMutation({
    mutationFn: createDemoUser,
    onSuccess: () => {
      toast.success("Demo account ready");
      setDemoEmail("");
      setDemoDisplayName("");
      queryClient.invalidateQueries({ queryKey: ["admin-users"] });
    },
    onError: (error: Error) => {
      toast.error(error.message || "Failed to create demo account");
    },
  });

  const seedScenarioMutation = useMutation({
    mutationFn: seedScenarioForUser,
    onSuccess: () => {
      toast.success("Demo scenario seeded");
    },
    onError: (error: Error) => {
      toast.error(error.message || "Failed to seed scenario");
    },
  });

  const impersonateMutation = useMutation({
    mutationFn: impersonateUser,
    onSuccess: () => {
      toast.success("Switched to demo account");
      window.location.assign("/dashboard");
    },
    onError: (error: Error) => {
      toast.error(error.message || "Failed to switch accounts");
    },
  });

  const resetDemoMutation = useMutation({
    mutationFn: (userId: number) => resetDemoUser(userId),
    onSuccess: (data) => {
      toast.success(`Demo reset for ${data.email}`);
      setDemoResetState({ isOpen: false, target: null });
      queryClient.invalidateQueries({ queryKey: ["admin-users"] });
    },
    onError: (error: Error) => {
      toast.error(error.message || "Failed to reset demo account");
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

  const handleCreateDemo = () => {
    createDemoMutation.mutate({
      email: demoEmail.trim() || undefined,
      display_name: demoDisplayName.trim() || undefined,
    });
  };

  const handleSeedDemo = (userId?: number | null) => {
    const targetId = userId ?? selectedDemoUserId;
    if (!targetId) {
      toast.error("Select a demo account first");
      return;
    }
    const demoUser = demoUsers.find((item) => item.id === targetId);
    if (!demoUser) {
      toast.error("Demo account not found");
      return;
    }
    const scenarioName = demoScenario.trim();
    if (!scenarioName) {
      toast.error("Enter a scenario name to seed");
      return;
    }
    seedScenarioMutation.mutate({
      name: scenarioName,
      owner_email: demoUser.email,
      clean: true,
    });
  };

  const handleImpersonate = (userId?: number | null) => {
    const targetId = userId ?? selectedDemoUserId;
    if (!targetId) {
      toast.error("Select a demo account first");
      return;
    }
    impersonateMutation.mutate({ user_id: targetId });
  };

  // One-click: seed sample data then impersonate
  const handleStartDemo = async (userId: number) => {
    const demoUser = demoUsers.find((item) => item.id === userId);
    if (!demoUser) {
      toast.error("Demo account not found");
      return;
    }

    try {
      // First seed the scenario
      await seedScenarioForUser({
        name: "swarm-mvp",
        owner_email: demoUser.email,
        clean: true,
      });

      // Then impersonate
      await impersonateUser({ user_id: userId });
      toast.success("Demo ready!");
      window.location.assign("/dashboard");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to start demo");
    }
  };

  const handleOpenDemoReset = (userId?: number | null) => {
    const targetId = userId ?? selectedDemoUserId;
    if (!targetId) {
      toast.error("Select a demo account first");
      return;
    }
    const demoUser = demoUsers.find((item) => item.id === targetId);
    if (!demoUser) {
      toast.error("Demo account not found");
      return;
    }
    setDemoResetState({ isOpen: true, target: demoUser });
  };

  const handleConfirmDemoReset = () => {
    if (!demoResetState.target) return;
    resetDemoMutation.mutate(demoResetState.target.id);
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
    <PageShell size="wide" className="admin-page-container">
      <SectionHeader
        title="Operations Dashboard"
        description="Monitor system-wide activity, budgets, and user usage."
        actions={
          <div className="window-selector">
            <label className="admin-window-label">Time Window:</label>
            <select
              className="ui-input admin-window-select"
              value={selectedWindow}
              onChange={(e) => setSelectedWindow(e.target.value as "today" | "7d" | "30d")}
            >
              <option value="today">Today</option>
              <option value="7d">Last 7 Days</option>
              <option value="30d">Last 30 Days</option>
            </select>
          </div>
        }
      />

      {summaryLoading ? (
        <EmptyState
          icon={<Spinner size="lg" />}
          title="Loading operations data..."
          description="Fetching real-time metrics."
        />
      ) : summaryError ? (
        <EmptyState
          variant="error"
          title="Error loading operations"
          description={String(summaryError)}
          action={<Button onClick={() => window.location.reload()}>Retry</Button>}
        />
      ) : summary ? (
        <div className="admin-stack">
          {/* Key Metrics - using real backend data */}
          <div className="metrics-grid">
            <MetricCard
              title="Runs Today"
              value={summary.runs_today}
              subtitle="Total executions"
              color={metricColors.primary}
            />
            <MetricCard
              title="Errors (1h)"
              value={summary.errors_last_hour}
              subtitle="Failed runs"
              color={metricColors.error}
            />
            <MetricCard
              title="Cost Today"
              value={summary.cost_today_usd !== null ? formatCurrency(summary.cost_today_usd) : "N/A"}
              subtitle="USD spent"
              color={metricColors.success}
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
                summary.budget_user.percent === null ? metricColors.muted :
                summary.budget_user.percent > 80 ? metricColors.error :
                summary.budget_user.percent > 60 ? metricColors.warning : metricColors.success
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
                summary.budget_global.percent === null ? metricColors.muted :
                summary.budget_global.percent > 80 ? metricColors.error :
                summary.budget_global.percent > 60 ? metricColors.warning : metricColors.success
              }
            />
            <MetricCard
              title="Latency P95"
              value={`${summary.latency_ms.p95}ms`}
              subtitle={`P50: ${summary.latency_ms.p50}ms`}
              color={metricColors.accent}
            />
          </div>

          {/* Top Agents Section - using data from summary */}
          <Card>
            <Card.Header>
              <h3 className="admin-section-title ui-section-title">Top Performing Agents (Today)</h3>
            </Card.Header>
            <Card.Body>
              <TopAgentsTable agents={summary.top_agents_today} />
            </Card.Body>
          </Card>

          {/* Users Usage Section */}
          <Card>
            <Card.Header>
              <h3 className="admin-section-title ui-section-title">User LLM Usage</h3>
            </Card.Header>
            <Card.Body>
              <p className="section-description admin-section-description">
                Click on a user to see detailed usage breakdown
              </p>
              {usersLoading ? (
                <div className="admin-empty-state">Loading users...</div>
              ) : usersData?.users ? (
                <UsersTable
                  users={usersData.users}
                  sortField={usersSortField}
                  sortOrder={usersSortOrder}
                  onSort={handleUsersSort}
                  onUserClick={handleUserClick}
                />
              ) : (
                <div className="admin-empty-state">No users found</div>
              )}
            </Card.Body>
          </Card>

          {/* Demo Mode */}
          <Card>
            <Card.Header>
              <h3 className="admin-section-title ui-section-title">Demo Mode</h3>
            </Card.Header>
            <Card.Body>
              <p className="section-description admin-section-description">
                One-click demo with sample data. Perfect for showing off Swarmlet.
              </p>

              {demoUsers.length === 0 ? (
                <div className="demo-empty-state">
                  <p>Create a demo account to show off Swarmlet with sample data.</p>
                  <Button
                    variant="primary"
                    size="lg"
                    onClick={handleCreateDemo}
                    disabled={createDemoMutation.isPending}
                  >
                    {createDemoMutation.isPending ? "Creating..." : "Create Demo Account"}
                  </Button>
                </div>
              ) : (
                <div className="demo-accounts-grid">
                  {demoUsers.map((demoUser) => {
                    // Extract just the unique part from email for cleaner display
                    const emailId = demoUser.email.split('@')[0].replace('demo+', '');
                    return (
                      <div key={demoUser.id} className="demo-account-card" style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        padding: '20px 24px',
                        background: 'linear-gradient(135deg, rgba(255,255,255,0.03) 0%, rgba(255,255,255,0.01) 100%)',
                        border: '1px solid rgba(255,255,255,0.1)',
                        borderRadius: '12px',
                      }}>
                        <div className="demo-account-info" style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                          <span className="demo-account-name" style={{ fontWeight: 600, fontSize: '16px', color: '#fff' }}>
                            {demoUser.display_name || "Demo Account"}
                          </span>
                          <span className="demo-account-email" style={{ fontSize: '13px', color: 'rgba(255,255,255,0.5)', fontFamily: 'monospace' }}>{emailId}</span>
                        </div>
                        <div className="demo-account-actions" style={{ display: 'flex', gap: '12px' }}>
                          <Button
                            variant="primary"
                            onClick={() => handleStartDemo(demoUser.id)}
                            disabled={seedScenarioMutation.isPending || impersonateMutation.isPending}
                          >
                            {(seedScenarioMutation.isPending || impersonateMutation.isPending)
                              ? "Starting..."
                              : "▶ Start Demo"}
                          </Button>
                          <Button
                            variant="ghost"
                            onClick={() => handleOpenDemoReset(demoUser.id)}
                            disabled={resetDemoMutation.isPending}
                          >
                            Clear
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                  <button
                    className="demo-add-button"
                    onClick={handleCreateDemo}
                    disabled={createDemoMutation.isPending}
                  >
                    + New demo account
                  </button>
                </div>
              )}
            </Card.Body>
          </Card>

          {/* System Information - using real backend data */}
          <Card>
            <Card.Header>
              <h3 className="admin-section-title ui-section-title">System Information</h3>
            </Card.Header>
            <Card.Body>
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
            </Card.Body>
          </Card>

          {/* Developer Tools */}
          <Card>
            <Card.Header>
              <h3 className="admin-section-title ui-section-title">Developer Tools</h3>
            </Card.Header>
            <Card.Body>
              <div className="admin-devtools-grid">
                <Link to="/traces" className="admin-devtools-link">
                  <div className="admin-devtool-card admin-devtool-card--trace">
                    <div className="admin-devtool-header">
                      <span className="admin-devtool-icon">🔍</span>
                      <span className="admin-devtool-title">Trace Explorer</span>
                    </div>
                    <p className="admin-devtool-desc">
                      Debug supervisor runs, workers, and LLM calls with unified trace timelines.
                    </p>
                  </div>
                </Link>

                <Link to="/reliability" className="admin-devtools-link">
                  <div className="admin-devtool-card admin-devtool-card--reliability">
                    <div className="admin-devtool-header">
                      <span className="admin-devtool-icon">📊</span>
                      <span className="admin-devtool-title">Reliability Dashboard</span>
                    </div>
                    <p className="admin-devtool-desc">
                      Monitor system reliability metrics, error rates, and performance trends.
                    </p>
                  </div>
                </Link>
              </div>
            </Card.Body>
          </Card>

          {/* Admin Actions */}
          <Card>
            <Card.Header>
              <h3 className="admin-section-title ui-section-title">Database Management</h3>
            </Card.Header>
            <Card.Body>
              <div className="admin-actions">
                <div className="action-group">
                  <Button
                    variant="danger"
                    onClick={handleClearData}
                    disabled={resetMutation.isPending}
                  >
                    Clear User Data
                  </Button>
                  <p className="action-description">
                    Remove all user-generated data (agents, runs, workflows) while preserving user accounts
                  </p>
                </div>
                <div className="action-group">
                  <Button
                    variant="danger"
                    onClick={handleFullReset}
                    disabled={resetMutation.isPending}
                  >
                    Full Database Reset
                  </Button>
                  <p className="action-description">
                    Drop and recreate all tables (destructive operation)
                  </p>
                </div>
              </div>
            </Card.Body>
          </Card>
        </div>
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

      <ConfirmationModal
        isOpen={demoResetState.isOpen}
        onClose={() => setDemoResetState({ isOpen: false, target: null })}
        onConfirm={handleConfirmDemoReset}
        title="Reset demo account"
        message={
          demoResetState.target
            ? `This will erase all data for ${demoResetState.target.email}. You can re-seed the baseline scenario afterwards.`
            : "This will erase all data for the selected demo account."
        }
        confirmText={resetDemoMutation.isPending ? "Resetting..." : "Reset demo"}
        isDangerous={true}
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
    </PageShell>
  );
}

export default AdminPage;
