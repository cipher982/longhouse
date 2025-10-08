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
