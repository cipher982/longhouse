/**
 * Jarvis API Client for Zerg Backend Integration
 *
 * Provides typed client for Jarvis-specific endpoints:
 * - Authentication (device secret → session cookie)
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

const JARVIS_SESSION_STORAGE_KEY = 'jarvis_session_meta';

export class JarvisAPIClient {
  private _baseURL: string;
  private sessionExpiresAt: number | null = null;
  private eventSource: EventSource | null = null;

  constructor(baseURL: string = 'http://localhost:47300') {
    this._baseURL = baseURL;

    // Load session metadata from localStorage if available
    const stored = localStorage.getItem(JARVIS_SESSION_STORAGE_KEY);
    if (stored) {
      try {
        const data = JSON.parse(stored);
        // Check if token is still valid (not expired)
        const now = Date.now() / 1000;
        if (typeof data.expires_at === 'number' && data.expires_at > now) {
          this.sessionExpiresAt = data.expires_at;
        } else {
          localStorage.removeItem(JARVIS_SESSION_STORAGE_KEY);
        }
      } catch (e) {
        localStorage.removeItem(JARVIS_SESSION_STORAGE_KEY);
      }
    }
  }

  /**
   * Get base URL
   */
  get baseURL(): string {
    return this._baseURL;
  }

  /**
   * Authenticate with device secret and receive JWT token
   */
  async authenticate(deviceSecret: string): Promise<JarvisAuthResponse> {
    const response = await fetch(`${this._baseURL}/api/jarvis/auth`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      credentials: 'include',
      body: JSON.stringify({ device_secret: deviceSecret }),
    });

    if (!response.ok) {
      throw new Error(`Authentication failed: ${response.statusText}`);
    }

    const data: JarvisAuthResponse = await response.json();

    // Store session expiry metadata (token itself remains HttpOnly)
    const expiresAt = Date.now() / 1000 + data.session_expires_in;
    this.sessionExpiresAt = expiresAt;
    try {
      localStorage.setItem(JARVIS_SESSION_STORAGE_KEY, JSON.stringify({
        expires_at: expiresAt,
      }));
    } catch {
      // Ignore storage errors (e.g. private mode)
    }

    return data;
  }

  /**
   * Check if client is authenticated
   */
  isAuthenticated(): boolean {
    if (this.sessionExpiresAt === null) {
      return false;
    }

    const now = Date.now() / 1000;
    if (this.sessionExpiresAt <= now) {
      this.resetSession();
      return false;
    }

    return true;
  }

  private ensureAuthenticated(): void {
    if (!this.isAuthenticated()) {
      throw new Error('Not authenticated - call authenticate() first');
    }
  }

  private resetSession(): void {
    this.sessionExpiresAt = null;
    try {
      localStorage.removeItem(JARVIS_SESSION_STORAGE_KEY);
    } catch {
      // Ignore storage errors (e.g. private mode)
    }
  }

  private async authenticatedFetch(input: RequestInfo, init: RequestInit = {}): Promise<Response> {
    this.ensureAuthenticated();
    const options: RequestInit = { ...init, credentials: 'include' };
    const response = await fetch(input, options);
    if (response.status === 401) {
      this.resetSession();
      throw new Error('Authentication expired');
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
  connectSupervisorEventStream(runId: number, handlers: SupervisorEventHandlers): void {
    this.ensureAuthenticated();

    // Close existing supervisor connection if any
    this.disconnectSupervisorEventStream();

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
        // Auto-close on completion
        this.disconnectSupervisorEventStream();
        handlers.onStreamClose?.();
      } catch (err) {
        console.error('Failed to parse supervisor_complete event:', err);
      }
    });

    this.supervisorEventSource.addEventListener('error', (e: MessageEvent) => {
      try {
        const event: SupervisorEvent = JSON.parse(e.data);
        handlers.onError?.(event);
        // Auto-close on error
        this.disconnectSupervisorEventStream();
        handlers.onStreamClose?.();
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

    return new Promise((resolve, reject) => {
      const timeoutId = setTimeout(() => {
        this.disconnectSupervisorEventStream();
        reject(new Error(`Supervisor task timed out after ${timeout}ms`));
      }, timeout);

      this.connectSupervisorEventStream(response.run_id, {
        onSupervisorStarted: (event) => {
          options?.onProgress?.(event);
        },
        onSupervisorThinking: (event) => {
          options?.onProgress?.(event);
        },
        onWorkerSpawned: (event) => {
          options?.onProgress?.(event);
        },
        onWorkerStarted: (event) => {
          options?.onProgress?.(event);
        },
        onWorkerComplete: (event) => {
          options?.onProgress?.(event);
        },
        onWorkerSummaryReady: (event) => {
          options?.onProgress?.(event);
        },
        onSupervisorComplete: (event) => {
          clearTimeout(timeoutId);
          const result = event.payload?.result || event.payload?.message || 'Task completed';
          resolve(result);
        },
        onError: (event) => {
          clearTimeout(timeoutId);
          const errorMsg = event.payload?.message || event.payload?.error || 'Supervisor task failed';
          reject(new Error(errorMsg));
        },
        onStreamError: (error) => {
          clearTimeout(timeoutId);
          reject(new Error('SSE stream error'));
        },
      });
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
    this.ensureAuthenticated();

    // Close existing connection if any
    this.disconnectEventStream();

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
   * Logout and clear stored token
   */
  logout(): void {
    this.resetSession();
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
