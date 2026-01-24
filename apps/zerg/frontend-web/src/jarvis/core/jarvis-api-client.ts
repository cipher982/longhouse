/**
 * Jarvis API Client for Zerg Backend Integration
 *
 * Provides typed client for Jarvis-specific endpoints:
 * - Authentication (HttpOnly cookie via swarmlet_session)
 * - Agent listing
 * - Run history
 * - Task dispatch
 * - SSE event streaming
 */

export interface JarvisAuthRequest {
  device_secret: string;
}

export interface JarvisAuthResponse {
  session_expires_in: number;
  session_cookie_name: string;
}

export interface JarvisAgentSummary {
  id: number;
  name: string;
  status: string;
  schedule?: string;
  next_run_at?: string;
  description?: string;
}

export interface JarvisRunSummary {
  id: number;
  agent_id: number;
  thread_id?: number;
  agent_name: string;
  status: string;
  summary?: string;
  signal?: string;
  signal_source?: string;
  error?: string;
  last_event_type?: string;
  last_event_message?: string;
  last_event_at?: string;
  continuation_of_run_id?: number;
  created_at: string;
  updated_at: string;
  completed_at?: string;
}

export interface JarvisDispatchRequest {
  agent_id: number;
  task_override?: string;
}

export interface JarvisDispatchResponse {
  run_id: number;
  thread_id: number;
  status: string;
  agent_name: string;
}

export interface JarvisEventData {
  type: string;
  payload: Record<string, any>;
  timestamp: string;
}

/**
 * Prepare fetch options for cookie-based auth.
 * Auth is now handled via HttpOnly swarmlet_session cookie.
 */
function withCookieAuth(init: RequestInit = {}): RequestInit {
  const headers = new Headers(init.headers ?? {});
  // No Authorization header needed - cookie is sent automatically
  return { ...init, credentials: 'include', headers };
}

export class JarvisAPIClient {
  private _baseURL: string;
  private eventSource: EventSource | null = null;

  constructor(baseURL: string = 'http://localhost:47300') {
    this._baseURL = baseURL;
  }

  /**
   * Get base URL
   */
  get baseURL(): string {
    return this._baseURL;
  }

  /**
   * Deprecated: Jarvis now uses HttpOnly cookie-based auth.
   * Login is handled by the main Swarmlet dashboard.
   */
  async authenticate(): Promise<never> {
    throw new Error('Deprecated: Jarvis uses HttpOnly cookie auth via Swarmlet dashboard login.');
  }

  /**
   * Check if the user is likely authenticated.
   *
   * Note: With HttpOnly cookies, we can't directly check auth status from JS.
   * This method attempts to verify by making a lightweight API call.
   * In dev mode (AUTH_DISABLED=1) the backend may accept requests without auth.
   */
  async isAuthenticated(): Promise<boolean> {
    try {
      const resp = await fetch(`${this._baseURL}/api/auth/verify`, {
        method: 'GET',
        credentials: 'include',
      });
      return resp.status === 204;
    } catch {
      return false;
    }
  }

  private async authenticatedFetch(input: RequestInfo, init: RequestInit = {}): Promise<Response> {
    const options = withCookieAuth(init);
    const response = await fetch(input, options);
    if (response.status === 401) {
      throw new Error('Not authenticated');
    }
    return response;
  }

  /**
   * List available agents
   */
  async listAgents(): Promise<JarvisAgentSummary[]> {
    const response = await this.authenticatedFetch(`${this._baseURL}/api/jarvis/agents`);

    if (!response.ok) {
      throw new Error(`Failed to list agents: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * Get recent agent runs
   */
  async listRuns(options?: { limit?: number; agent_id?: number }): Promise<JarvisRunSummary[]> {
    const params = new URLSearchParams();
    if (options?.limit) params.append('limit', options.limit.toString());
    if (options?.agent_id) params.append('agent_id', options.agent_id.toString());

    const url = `${this._baseURL}/api/jarvis/runs${params.toString() ? '?' + params.toString() : ''}`;

    const response = await this.authenticatedFetch(url);

    if (!response.ok) {
      throw new Error(`Failed to list runs: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * Dispatch agent task
   */
  async dispatch(request: JarvisDispatchRequest): Promise<JarvisDispatchResponse> {
    const response = await this.authenticatedFetch(
      `${this._baseURL}/api/jarvis/dispatch`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(request),
      },
    );

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(`Failed to dispatch agent: ${error.detail}`);
    }

    return response.json();
  }

  // ---------------------------------------------------------------------------
  // Supervisor Methods
  // ---------------------------------------------------------------------------

  /**
   * Cancel a running supervisor task
   */
  async cancelSupervisor(runId: number): Promise<{ run_id: number; status: string; message: string }> {
    const response = await this.authenticatedFetch(
      `${this._baseURL}/api/jarvis/supervisor/${runId}/cancel`,
      {
        method: 'POST',
      },
    );

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(`Failed to cancel supervisor: ${error.detail}`);
    }

    return response.json();
  }

  /**
   * Connect to SSE event stream
   */
  connectEventStream(handlers: {
    onConnected?: () => void;
    onHeartbeat?: (timestamp: string) => void;
    onAgentUpdated?: (event: JarvisEventData) => void;
    onRunCreated?: (event: JarvisEventData) => void;
  onRunUpdated?: (event: JarvisEventData) => void;
  onError?: (error: Event) => void;
}): void {
    // Close existing connection if any
    this.disconnectEventStream();

    // Cookie-based auth - withCredentials: true sends HttpOnly session cookie
    // In E2E tests, include worker ID query param for DB schema isolation
    const testWorkerId = typeof window !== 'undefined' ? (window as any).__TEST_WORKER_ID__ : undefined;
    const url = `${this._baseURL}/api/jarvis/events${testWorkerId ? `?worker=${testWorkerId}` : ''}`;
    this.eventSource = new EventSource(url, { withCredentials: true });

    this.eventSource.addEventListener('connected', () => {
      handlers.onConnected?.();
    });

    this.eventSource.addEventListener('heartbeat', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data);
        handlers.onHeartbeat?.(data.timestamp);
      } catch (err) {
        console.error('Failed to parse heartbeat:', err);
      }
    });

    this.eventSource.addEventListener('agent_updated', (e: MessageEvent) => {
      try {
        const event: JarvisEventData = JSON.parse(e.data);
        handlers.onAgentUpdated?.(event);
      } catch (err) {
        console.error('Failed to parse agent_updated event:', err);
      }
    });

    this.eventSource.addEventListener('run_created', (e: MessageEvent) => {
      try {
        const event: JarvisEventData = JSON.parse(e.data);
        handlers.onRunCreated?.(event);
      } catch (err) {
        console.error('Failed to parse run_created event:', err);
      }
    });

    this.eventSource.addEventListener('run_updated', (e: MessageEvent) => {
      try {
        const event: JarvisEventData = JSON.parse(e.data);
        handlers.onRunUpdated?.(event);
      } catch (err) {
        console.error('Failed to parse run_updated event:', err);
      }
    });

    this.eventSource.onerror = (error) => {
      handlers.onError?.(error);
      // Auto-reconnect logic could be added here
    };
  }

  /**
   * Disconnect from SSE event stream
   */
  disconnectEventStream(): void {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }

  /**
   * Logout (disconnects streams).
   * Cookie-based auth is managed by the server via /api/auth/logout.
   */
  logout(): void {
    this.disconnectEventStream();
  }
}

// Singleton instance
let clientInstance: JarvisAPIClient | null = null;

/**
 * Get or create Jarvis API client instance
 */
export function getJarvisClient(baseURL?: string): JarvisAPIClient {
  if (!clientInstance || (baseURL && clientInstance.baseURL !== baseURL)) {
    clientInstance = new JarvisAPIClient(baseURL);
  }
  return clientInstance;
}
