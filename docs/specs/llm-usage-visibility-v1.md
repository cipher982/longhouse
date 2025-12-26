# LLM Usage Visibility Spec v1

**Status:** Draft
**Author:** AI Assistant
**Date:** 2024-12-17
**Related:** (none)

---

## Problem Statement

Users have no visibility into their LLM token/cost usage. They discover limits only when blocked (HTTP 429). Admins can see aggregate platform costs but cannot identify individual user spending patterns or potential abusers.

**Current state:**

- ✅ Token/cost tracking per run (`AgentRun.total_tokens`, `total_cost_usd`)
- ✅ Daily budget enforcement (`DAILY_COST_PER_USER_CENTS`)
- ✅ Admin aggregate dashboard (`/api/ops/summary`)
- ❌ No user-facing usage endpoint
- ❌ No user-facing usage UI
- ❌ No per-user breakdown for admins
- ❌ No proactive warnings before limit hit

---

## Goals

1. **Users can see their own LLM usage** - tokens, cost, budget remaining
2. **Users get warned before hitting limits** - 80% threshold toast
3. **Admins can see per-user cost breakdowns** - identify heavy users
4. **Simple implementation** - DB aggregation, no new infrastructure

## Non-Goals

- Billing/invoicing (no real money changing hands yet)
- Per-model breakdowns (nice-to-have, not P0)
- Historical trends beyond 30 days
- Rate limiting (separate concern, already exists)
- Redis/external caching (DB aggregation is sufficient at current scale)

---

## User Stories

### Users

1. As a user, I want to see how much I've spent today so I can pace my usage
2. As a user, I want to see my remaining budget before I run out
3. As a user, I want a warning when I'm approaching my limit (not just a hard block)

### Admins

1. As an admin, I want to see which users are spending the most
2. As an admin, I want to quickly identify if someone is abusing the platform
3. As an admin, I want to see a user's usage history (7d, 30d)

---

## API Design

### 1. User Usage Endpoint

**`GET /api/users/me/usage`**

Returns the authenticated user's LLM usage for the requested period.

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `period` | enum | `today` | One of: `today`, `7d`, `30d` |

**Response (200):**

```json
{
  "period": "today",
  "tokens": {
    "prompt": 12345,
    "completion": 6789,
    "total": 19134
  },
  "cost_usd": 0.0234,
  "runs": 15,
  "limit": {
    "daily_cost_cents": 100,
    "used_percent": 23.4,
    "remaining_usd": 0.0766,
    "status": "ok"
  }
}
```

**`limit.status` enum:**

- `ok` - Under 80% of limit
- `warning` - Between 80-99% of limit
- `exceeded` - At or over limit
- `unlimited` - No limit configured (`daily_cost_cents = 0`)

**Notes:**

- `tokens.prompt` and `tokens.completion` are summed from `AgentRun` rows where the underlying runs captured this breakdown. May be `null` if not available.
- `cost_usd` sums `AgentRun.total_cost_usd` (NULL costs excluded).
- `runs` is count of `AgentRun` started in period (for informational display only).
- For `7d`/`30d` periods, `limit` still reflects today's daily limit usage (budgets are daily).

**Errors:**

- 401: Not authenticated

---

### 2. Admin Users List with Usage

**`GET /api/admin/users`**

Returns all users with their usage stats. Admin-only.

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `sort` | enum | `cost_today` | Sort by: `cost_today`, `cost_7d`, `cost_30d`, `email`, `created_at` |
| `order` | enum | `desc` | `asc` or `desc` |
| `limit` | int | 50 | Max users to return |
| `offset` | int | 0 | Pagination offset |

**Response (200):**

```json
{
  "users": [
    {
      "id": 1,
      "email": "heavy@user.com",
      "display_name": "Heavy User",
      "role": "USER",
      "is_active": true,
      "created_at": "2024-01-15T10:30:00Z",
      "usage": {
        "today": {
          "tokens": 150000,
          "cost_usd": 0.45,
          "runs": 42
        },
        "7d": {
          "tokens": 1050000,
          "cost_usd": 3.2,
          "runs": 280
        },
        "30d": {
          "tokens": 4200000,
          "cost_usd": 12.5,
          "runs": 1100
        }
      }
    }
  ],
  "total": 125,
  "limit": 50,
  "offset": 0
}
```

**Errors:**

- 401: Not authenticated
- 403: Not admin

---

### 3. Admin Single User Usage Detail

**`GET /api/admin/users/{user_id}/usage`**

Returns detailed usage for a specific user. Admin-only.

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `period` | enum | `today` | One of: `today`, `7d`, `30d` |

**Response (200):**

```json
{
  "user": {
    "id": 1,
    "email": "user@example.com",
    "display_name": "Example User",
    "role": "USER",
    "is_active": true
  },
  "period": "7d",
  "summary": {
    "tokens": {
      "prompt": 420000,
      "completion": 180000,
      "total": 600000
    },
    "cost_usd": 1.85,
    "runs": 156
  },
  "daily_breakdown": [
    { "date": "2024-12-17", "tokens": 85000, "cost_usd": 0.26, "runs": 22 },
    { "date": "2024-12-16", "tokens": 92000, "cost_usd": 0.28, "runs": 25 },
    { "date": "2024-12-15", "tokens": 78000, "cost_usd": 0.24, "runs": 20 }
  ],
  "top_agents": [
    {
      "agent_id": 42,
      "name": "Research Bot",
      "tokens": 320000,
      "cost_usd": 0.98,
      "runs": 80
    },
    {
      "agent_id": 15,
      "name": "Email Assistant",
      "tokens": 180000,
      "cost_usd": 0.55,
      "runs": 45
    }
  ]
}
```

**Errors:**

- 401: Not authenticated
- 403: Not admin
- 404: User not found

---

## Frontend Design

### 1. Usage Widget (User Dashboard / Profile)

Display on the main dashboard or profile page.

```
┌─────────────────────────────────────────────┐
│ 📊 Today's LLM Usage                        │
├─────────────────────────────────────────────┤
│                                             │
│  ████████████████████░░░░░  78%             │
│                                             │
│  $0.078 of $0.10 daily limit                │
│  19,134 tokens · 15 runs                    │
│                                             │
│  ⚠️ Approaching daily limit                 │  (only when status=warning)
└─────────────────────────────────────────────┘
```

**Behavior:**

- Shows on dashboard and/or profile page
- Progress bar color:
  - Green: 0-60%
  - Yellow: 60-80%
  - Orange: 80-95%
  - Red: 95-100%
- Warning banner only when `status === 'warning'`
- Clicking opens expanded view (optional)

### 2. Usage Warning Toast

When a user's usage crosses 80%, show a non-blocking toast notification.

```
┌────────────────────────────────────────┐
│ ⚠️ You've used 80% of your daily      │
│    LLM budget ($0.08 of $0.10).        │
│    Usage resets at midnight UTC.       │
│                              [Dismiss] │
└────────────────────────────────────────┘
```

**Trigger logic:**

- On each run completion, check if crossed 80% threshold
- Store `warned_today` flag in localStorage to avoid repeated toasts
- Reset flag at midnight UTC

**Implementation options:**

1. **Polling:** Widget polls `/api/users/me/usage` every 60s, compares to previous
2. **WebSocket:** Add `user_usage_warning` event type (cleaner but more work)
3. **Response header:** Backend returns `X-Usage-Warning: 80` header when approaching limit

Recommendation: Start with **polling** (option 1) - simplest, no backend changes beyond the endpoint.

### 3. Admin Users Table

New section on Admin page or dedicated route (`/admin/users`).

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 👥 Users by LLM Cost                                    [Today ▼] [Search] │
├─────────────────────────────────────────────────────────────────────────────┤
│ Email                    │ Today      │ 7 Days     │ 30 Days    │ Status   │
├─────────────────────────────────────────────────────────────────────────────┤
│ heavy@user.com           │ $0.45      │ $3.20      │ $12.50     │ 🟢 Active │
│ moderate@user.com        │ $0.12      │ $0.85      │ $3.40      │ 🟢 Active │
│ light@user.com           │ $0.02      │ $0.15      │ $0.60      │ 🟢 Active │
│ inactive@user.com        │ $0.00      │ $0.00      │ $0.00      │ 🔴 Inactive│
└─────────────────────────────────────────────────────────────────────────────┘
```

**Features:**

- Sortable columns (click header)
- Click row → expand or navigate to user detail
- Search by email
- Filter by: active/inactive, role

### 4. Admin User Detail Modal/Page

When clicking a user row:

```
┌─────────────────────────────────────────────────────────────────┐
│ 👤 heavy@user.com                                    [× Close]  │
├─────────────────────────────────────────────────────────────────┤
│ Display Name: Heavy User                                        │
│ Role: USER                                                      │
│ Member Since: Jan 15, 2024                                      │
│ Status: 🟢 Active                                               │
├─────────────────────────────────────────────────────────────────┤
│ 📊 Usage (Last 7 Days)                                          │
│                                                                 │
│ Total Cost: $3.20                                               │
│ Total Tokens: 1,050,000                                         │
│ Total Runs: 280                                                 │
│                                                                 │
│ Daily Breakdown:                                                │
│ ┌───────────────────────────────────────────────────────────┐   │
│ │ Dec 17  ████████████  $0.45                               │   │
│ │ Dec 16  ██████████    $0.38                               │   │
│ │ Dec 15  ████████      $0.32                               │   │
│ │ ...                                                       │   │
│ └───────────────────────────────────────────────────────────┘   │
│                                                                 │
│ Top Agents:                                                     │
│ • Research Bot: $0.98 (80 runs)                                 │
│ • Email Assistant: $0.55 (45 runs)                              │
├─────────────────────────────────────────────────────────────────┤
│ [Block User]  [View Agents]  [View Runs]                        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Database Queries

### User's Today Usage

```sql
SELECT
  COALESCE(SUM(ar.total_tokens), 0) as total_tokens,
  COALESCE(SUM(ar.total_cost_usd), 0) as cost_usd,
  COUNT(ar.id) as runs
FROM agent_runs ar
JOIN agents a ON a.id = ar.agent_id
WHERE a.owner_id = :user_id
  AND ar.started_at IS NOT NULL
  AND DATE(ar.started_at) = CURRENT_DATE  -- UTC
```

### User's 7d/30d Usage

Same query with `DATE(ar.started_at) >= CURRENT_DATE - INTERVAL '7 days'`.

### All Users with Usage (Admin)

```sql
SELECT
  u.id,
  u.email,
  u.display_name,
  u.role,
  u.is_active,
  u.created_at,
  -- Today
  COALESCE(SUM(CASE WHEN DATE(ar.started_at) = CURRENT_DATE THEN ar.total_tokens END), 0) as tokens_today,
  COALESCE(SUM(CASE WHEN DATE(ar.started_at) = CURRENT_DATE THEN ar.total_cost_usd END), 0) as cost_today,
  -- 7d
  COALESCE(SUM(CASE WHEN ar.started_at >= CURRENT_DATE - 7 THEN ar.total_tokens END), 0) as tokens_7d,
  COALESCE(SUM(CASE WHEN ar.started_at >= CURRENT_DATE - 7 THEN ar.total_cost_usd END), 0) as cost_7d,
  -- 30d
  COALESCE(SUM(CASE WHEN ar.started_at >= CURRENT_DATE - 30 THEN ar.total_tokens END), 0) as tokens_30d,
  COALESCE(SUM(CASE WHEN ar.started_at >= CURRENT_DATE - 30 THEN ar.total_cost_usd END), 0) as cost_30d
FROM users u
LEFT JOIN agents a ON a.owner_id = u.id
LEFT JOIN agent_runs ar ON ar.agent_id = a.id AND ar.started_at >= CURRENT_DATE - 30
GROUP BY u.id
ORDER BY cost_today DESC
LIMIT :limit OFFSET :offset
```

**Performance notes:**

- Query scans 30 days of `agent_runs` but this is acceptable at current scale (<1000 users, <100k runs/month)
- Index exists on `agent_runs.started_at`
- If performance degrades: add daily materialized rollup table

---

## Implementation Plan

### Phase 1: User Self-Service (P0)

**Backend:**

1. Add `GET /api/users/me/usage` endpoint in `routers/users.py`
2. Add service function `get_user_usage(db, user_id, period)` in `services/usage_service.py`
3. Add Pydantic response models in `apps/zerg/backend/zerg/schemas/usage.py`

**Frontend:** 4. Add `UsageWidget` component 5. Add to Dashboard and/or Profile page 6. Implement polling (60s interval) 7. Add warning toast logic (80% threshold, localStorage debounce)

### Phase 2: Admin Per-User View (P1)

**Backend:**

1. Add `GET /api/admin/users` endpoint in `routers/admin.py`
2. Add `GET /api/admin/users/{id}/usage` endpoint
3. Add admin-only service functions

**Frontend:** 4. Add Users table to Admin page (or new route) 5. Add user detail modal/slide-out

### Phase 3: Enhancements (P2)

- Per-model token/cost breakdown
- User blocking from admin UI
- Per-user quota overrides (stored in `users.prefs`)
- Email alerts when approaching limit

---

## Configuration

No new environment variables required. Uses existing:

```bash
# Already exists
DAILY_COST_PER_USER_CENTS=100   # $1.00/day per user
DAILY_COST_GLOBAL_CENTS=1000    # $10.00/day platform total
```

---

## Testing Plan

### Backend Unit Tests

```python
# tests/test_user_usage.py

def test_usage_endpoint_returns_today_stats(db_session, client, auth_headers):
    """Verify /api/users/me/usage returns correct aggregates."""
    # Create runs with known costs
    # Assert response matches expected totals

def test_usage_limit_status_ok_under_80_percent(db_session, client):
    """status='ok' when under 80% of limit."""

def test_usage_limit_status_warning_over_80_percent(db_session, client):
    """status='warning' when 80-99% of limit."""

def test_usage_limit_status_exceeded_at_100_percent(db_session, client):
    """status='exceeded' when at or over limit."""

def test_usage_limit_status_unlimited_when_no_limit(db_session, client):
    """status='unlimited' when DAILY_COST_PER_USER_CENTS=0."""

def test_usage_7d_period(db_session, client):
    """Verify 7d aggregation includes correct date range."""

def test_admin_users_list_requires_admin(client, user_auth_headers):
    """Non-admin gets 403 on /api/admin/users."""

def test_admin_users_list_sorted_by_cost(db_session, admin_client):
    """Users returned in descending cost order."""
```

### Frontend Tests

- UsageWidget renders correct percentage
- Progress bar color matches threshold
- Warning toast appears at 80%
- Toast only shown once per day (localStorage)
- Admin table sorts correctly

---

## Security Considerations

1. **User isolation:** `/api/users/me/usage` only returns authenticated user's data
2. **Admin protection:** `/api/admin/users*` requires `role=ADMIN`
3. **No PII exposure:** Usage stats are numeric aggregates, no message content
4. **Rate limiting:** Existing rate limiter applies (no new concerns)

---

## Rollout Plan

1. **Deploy backend** - endpoints available immediately
2. **Deploy frontend** - widgets visible to all users
3. **Monitor** - watch for performance issues on admin query
4. **Iterate** - add per-model breakdown if requested

---

## Open Questions

1. **Where to show usage widget?** Dashboard, Profile, both, or dedicated Usage page?
   - Recommendation: Dashboard (most visible) + Profile (expected location)

2. **Should we add per-user quota overrides?**
   - Recommendation: Defer to P2. Store in `users.prefs` JSON when needed.

3. **Should we send email alerts?**
   - Recommendation: Defer to P2. Discord alerts to admin are sufficient for now.

---

## Appendix: Pydantic Models

```python
# schemas/usage.py

from pydantic import BaseModel
from typing import Optional, Literal
from datetime import date

class TokenBreakdown(BaseModel):
    prompt: Optional[int] = None
    completion: Optional[int] = None
    total: int

class UsageLimit(BaseModel):
    daily_cost_cents: int
    used_percent: float
    remaining_usd: float
    status: Literal["ok", "warning", "exceeded", "unlimited"]

class UserUsageResponse(BaseModel):
    period: Literal["today", "7d", "30d"]
    tokens: TokenBreakdown
    cost_usd: float
    runs: int
    limit: UsageLimit

class UserUsageSummary(BaseModel):
    today: dict  # {tokens, cost_usd, runs}
    seven_days: dict  # renamed from 7d for Python compat
    thirty_days: dict

class AdminUserRow(BaseModel):
    id: int
    email: str
    display_name: Optional[str]
    role: str
    is_active: bool
    created_at: datetime
    usage: UserUsageSummary

class AdminUsersResponse(BaseModel):
    users: list[AdminUserRow]
    total: int
    limit: int
    offset: int

class DailyBreakdown(BaseModel):
    date: date
    tokens: int
    cost_usd: float
    runs: int

class TopAgentUsage(BaseModel):
    agent_id: int
    name: str
    tokens: int
    cost_usd: float
    runs: int

class AdminUserDetailResponse(BaseModel):
    user: AdminUserRow
    period: str
    summary: dict
    daily_breakdown: list[DailyBreakdown]
    top_agents: list[TopAgentUsage]
```
