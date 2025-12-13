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
  agent_name: string;
  status: string;
  summary?: string;
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

export interface JarvisSupervisorRequest {
  task: string;
  context?: {
    conversation_id?: string;
    previous_messages?: string[];
  };
  preferences?: {
    verbosity?: 'minimal' | 'normal' | 'verbose';
    notify_on_complete?: boolean;
  };
}

export interface JarvisSupervisorResponse {
  run_id: number;
  thread_id: number;
  status: string;
  stream_url: string;
}

export interface SupervisorEvent {
  type: string;
  payload: Record<string, any>;
  seq: number;
  timestamp: string;
}

export interface SupervisorEventHandlers {
  onConnected?: (data: { run_id: number; seq: number }) => void;
  onSupervisorStarted?: (event: SupervisorEvent) => void;
  onSupervisorThinking?: (event: SupervisorEvent) => void;
  onWorkerSpawned?: (event: SupervisorEvent) => void;
  onWorkerStarted?: (event: SupervisorEvent) => void;
  onWorkerComplete?: (event: SupervisorEvent) => void;
  onWorkerSummaryReady?: (event: SupervisorEvent) => void;
  onSupervisorComplete?: (event: SupervisorEvent) => void;
  onError?: (event: SupervisorEvent) => void;
  onHeartbeat?: (seq: number, timestamp: string) => void;
  onStreamError?: (error: Event) => void;
  onStreamClose?: () => void;
}

export interface JarvisEventData {
  type: string;
  payload: Record<string, any>;
  timestamp: string;
}

// Session cookie name (for reference only - cookie is HttpOnly and managed by server)
const SESSION_COOKIE_NAME = 'swarmlet_session';

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
   * Dispatch a task to the supervisor agent
   */
  async dispatchSupervisor(request: JarvisSupervisorRequest): Promise<JarvisSupervisorResponse> {
    const response = await this.authenticatedFetch(
      `${this._baseURL}/api/jarvis/supervisor`,
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
      throw new Error(`Failed to dispatch supervisor: ${error.detail}`);
    }

    return response.json();
  }

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

  private supervisorEventSource: EventSource | null = null;

  /**
   * Connect to supervisor SSE event stream for a specific run
   *
   * Returns a promise that resolves with the final result when supervisor completes,
   * or rejects on error.
   */
  connectSupervisorEventStream(
    runId: number,
    handlers: SupervisorEventHandlers,
    options?: { closeOnComplete?: boolean },
  ): void {
    // Close existing supervisor connection if any
    this.disconnectSupervisorEventStream();

    // Cookie-based auth - withCredentials: true sends HttpOnly session cookie
    const url = `${this._baseURL}/api/jarvis/supervisor/events?run_id=${runId}`;
    this.supervisorEventSource = new EventSource(url, { withCredentials: true });

    this.supervisorEventSource.addEventListener('connected', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data);
        handlers.onConnected?.(data);
      } catch (err) {
        console.error('Failed to parse connected event:', err);
      }
    });

    this.supervisorEventSource.addEventListener('supervisor_started', (e: MessageEvent) => {
      try {
        const event: SupervisorEvent = JSON.parse(e.data);
        handlers.onSupervisorStarted?.(event);
      } catch (err) {
        console.error('Failed to parse supervisor_started event:', err);
      }
    });

    this.supervisorEventSource.addEventListener('supervisor_thinking', (e: MessageEvent) => {
      try {
        const event: SupervisorEvent = JSON.parse(e.data);
        handlers.onSupervisorThinking?.(event);
      } catch (err) {
        console.error('Failed to parse supervisor_thinking event:', err);
      }
    });

    this.supervisorEventSource.addEventListener('worker_spawned', (e: MessageEvent) => {
      try {
        const event: SupervisorEvent = JSON.parse(e.data);
        handlers.onWorkerSpawned?.(event);
      } catch (err) {
        console.error('Failed to parse worker_spawned event:', err);
      }
    });

    this.supervisorEventSource.addEventListener('worker_started', (e: MessageEvent) => {
      try {
        const event: SupervisorEvent = JSON.parse(e.data);
        handlers.onWorkerStarted?.(event);
      } catch (err) {
        console.error('Failed to parse worker_started event:', err);
      }
    });

    this.supervisorEventSource.addEventListener('worker_complete', (e: MessageEvent) => {
      try {
        const event: SupervisorEvent = JSON.parse(e.data);
        handlers.onWorkerComplete?.(event);
      } catch (err) {
        console.error('Failed to parse worker_complete event:', err);
      }
    });

    this.supervisorEventSource.addEventListener('worker_summary_ready', (e: MessageEvent) => {
      try {
        const event: SupervisorEvent = JSON.parse(e.data);
        handlers.onWorkerSummaryReady?.(event);
      } catch (err) {
        console.error('Failed to parse worker_summary_ready event:', err);
      }
    });

    this.supervisorEventSource.addEventListener('supervisor_complete', (e: MessageEvent) => {
      try {
        const event: SupervisorEvent = JSON.parse(e.data);
        handlers.onSupervisorComplete?.(event);
        if (options?.closeOnComplete !== false) {
          this.disconnectSupervisorEventStream();
          handlers.onStreamClose?.();
        }
      } catch (err) {
        console.error('Failed to parse supervisor_complete event:', err);
      }
    });

    this.supervisorEventSource.addEventListener('error', (e: MessageEvent) => {
      try {
        const event: SupervisorEvent = JSON.parse(e.data);
        handlers.onError?.(event);
        if (options?.closeOnComplete !== false) {
          this.disconnectSupervisorEventStream();
          handlers.onStreamClose?.();
        }
      } catch (err) {
        console.error('Failed to parse error event:', err);
      }
    });

    this.supervisorEventSource.addEventListener('heartbeat', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data);
        handlers.onHeartbeat?.(data.seq, data.timestamp);
      } catch (err) {
        console.error('Failed to parse heartbeat event:', err);
      }
    });

    this.supervisorEventSource.onerror = (error) => {
      handlers.onStreamError?.(error);
      if (options?.closeOnComplete !== false) {
        handlers.onStreamClose?.();
      }
    };
  }

  /**
   * Execute a supervisor task and wait for completion
   *
   * This is a convenience method that dispatches a task and waits for the result.
   * Returns the final result text or throws on error.
   */
  async executeSupervisorTask(task: string, options?: {
    timeout?: number;
    onProgress?: (event: SupervisorEvent) => void;
  }): Promise<string> {
    const timeout = options?.timeout ?? 120000; // 2 minute default timeout

    // Dispatch the task
    const response = await this.dispatchSupervisor({ task });

    // Emit a synthetic supervisor_started immediately so the UI has a real run_id
    // even if the SSE subscription misses the earliest events due to race conditions.
    options?.onProgress?.({
      type: 'supervisor_started',
      payload: { run_id: response.run_id, task },
      seq: 0,
      timestamp: new Date().toISOString(),
    });

    return new Promise((resolve, reject) => {
      let resolved = false;
      let pendingWorkers = 0;
      let supervisorCompleteEvent: SupervisorEvent | null = null;
      const timeoutId = setTimeout(() => {
        this.disconnectSupervisorEventStream();
        reject(new Error(`Supervisor task timed out after ${timeout}ms`));
      }, timeout);

      const doResolve = (forceComplete = false) => {
        if (resolved) return;
        // Normal resolution: supervisor complete and all workers finished
        // Forced resolution: stream closed, supervisor said it's done (workers may be orphaned)
        if (supervisorCompleteEvent && (pendingWorkers === 0 || forceComplete)) {
          if (forceComplete && pendingWorkers > 0) {
            console.warn(
              `[JarvisClient] Stream closed with ${pendingWorkers} pending workers. ` +
              'Some workers may have crashed without emitting worker_complete.',
            );
          }
          resolved = true;
          clearTimeout(timeoutId);
          const result =
            supervisorCompleteEvent.payload?.result ||
            supervisorCompleteEvent.payload?.message ||
            'Task completed';
          resolve(result);
          this.disconnectSupervisorEventStream();
        }
      };

      this.connectSupervisorEventStream(response.run_id, {
        onSupervisorStarted: (event) => {
          options?.onProgress?.(event);
        },
        onSupervisorThinking: (event) => {
          options?.onProgress?.(event);
        },
        onWorkerSpawned: (event) => {
          options?.onProgress?.(event);
          pendingWorkers += 1;
        },
        onWorkerStarted: (event) => {
          options?.onProgress?.(event);
        },
        onWorkerComplete: (event) => {
          options?.onProgress?.(event);
          pendingWorkers = Math.max(0, pendingWorkers - 1);
          doResolve();
        },
        onWorkerSummaryReady: (event) => {
          options?.onProgress?.(event);
          // worker_summary_ready indicates worker finished processing
          // Decrement here too in case worker_complete was missed
          if (pendingWorkers > 0) {
            pendingWorkers = Math.max(0, pendingWorkers - 1);
            doResolve();
          }
        },
        onSupervisorComplete: (event) => {
          supervisorCompleteEvent = event;
          doResolve();
        },
        onError: (event) => {
          if (resolved) return;
          resolved = true;
          clearTimeout(timeoutId);
          const errorMsg = event.payload?.message || event.payload?.error || 'Supervisor task failed';
          this.disconnectSupervisorEventStream();
          reject(new Error(errorMsg));
        },
        onStreamError: (error) => {
          if (resolved) return;
          resolved = true;
          clearTimeout(timeoutId);
          this.disconnectSupervisorEventStream();
          reject(new Error('SSE stream error'));
        },
        onStreamClose: () => {
          // Stream closed - if supervisor completed, resolve even with pending workers
          // This handles the case where workers crashed without emitting worker_complete
          if (supervisorCompleteEvent) {
            doResolve(true);
          } else if (!resolved) {
            // Stream closed before supervisor_complete - unexpected
            resolved = true;
            clearTimeout(timeoutId);
            reject(new Error('SSE stream closed before supervisor completed'));
          }
        },
      }, { closeOnComplete: false });
    });
  }

  /**
   * Disconnect from supervisor SSE event stream
   */
  disconnectSupervisorEventStream(): void {
    if (this.supervisorEventSource) {
      this.supervisorEventSource.close();
      this.supervisorEventSource = null;
    }
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
    const url = `${this._baseURL}/api/jarvis/events`;
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
    this.disconnectSupervisorEventStream();
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
