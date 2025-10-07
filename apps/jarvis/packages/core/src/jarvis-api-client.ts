/**
 * Jarvis API Client for Zerg Backend Integration
 *
 * Provides typed client for Jarvis-specific endpoints:
 * - Authentication (device secret → JWT)
 * - Agent listing
 * - Run history
 * - Task dispatch
 * - SSE event streaming
 */

export interface JarvisAuthRequest {
  device_secret: string;
}

export interface JarvisAuthResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
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

export class JarvisAPIClient {
  private _baseURL: string;
  private token: string | null = null;
  private eventSource: EventSource | null = null;

  constructor(baseURL: string = 'http://localhost:47300') {
    this._baseURL = baseURL;

    // Load token from localStorage if available
    const stored = localStorage.getItem('jarvis_token');
    if (stored) {
      try {
        const data = JSON.parse(stored);
        // Check if token is still valid (not expired)
        const now = Date.now() / 1000;
        if (data.expires_at > now) {
          this.token = data.access_token;
        } else {
          localStorage.removeItem('jarvis_token');
        }
      } catch (e) {
        localStorage.removeItem('jarvis_token');
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
      body: JSON.stringify({ device_secret: deviceSecret }),
    });

    if (!response.ok) {
      throw new Error(`Authentication failed: ${response.statusText}`);
    }

    const data: JarvisAuthResponse = await response.json();

    // Store token with expiry
    this.token = data.access_token;
    const expiresAt = Date.now() / 1000 + data.expires_in;
    localStorage.setItem('jarvis_token', JSON.stringify({
      access_token: data.access_token,
      expires_at: expiresAt,
    }));

    return data;
  }

  /**
   * Check if client is authenticated
   */
  isAuthenticated(): boolean {
    return this.token !== null;
  }

  /**
   * Get authorization header
   */
  private getAuthHeader(): Record<string, string> {
    if (!this.token) {
      throw new Error('Not authenticated - call authenticate() first');
    }
    return {
      'Authorization': `Bearer ${this.token}`,
    };
  }

  /**
   * List available agents
   */
  async listAgents(): Promise<JarvisAgentSummary[]> {
    const response = await fetch(`${this._baseURL}/api/jarvis/agents`, {
      headers: {
        ...this.getAuthHeader(),
      },
    });

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

    const response = await fetch(url, {
      headers: {
        ...this.getAuthHeader(),
      },
    });

    if (!response.ok) {
      throw new Error(`Failed to list runs: ${response.statusText}`);
    }

    return response.json();
  }

  /**
   * Dispatch agent task
   */
  async dispatch(request: JarvisDispatchRequest): Promise<JarvisDispatchResponse> {
    const response = await fetch(`${this._baseURL}/api/jarvis/dispatch`, {
      method: 'POST',
      headers: {
        ...this.getAuthHeader(),
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(request),
    });

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
    if (!this.token) {
      throw new Error('Not authenticated - call authenticate() first');
    }

    // Close existing connection if any
    this.disconnectEventStream();

    // Include token as query parameter since EventSource doesn't support custom headers
    const url = `${this._baseURL}/api/jarvis/events?token=${encodeURIComponent(this.token)}`;
    this.eventSource = new EventSource(url);

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
    this.token = null;
    localStorage.removeItem('jarvis_token');
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
