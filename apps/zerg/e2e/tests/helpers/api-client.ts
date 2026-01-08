import { testLog } from './test-logger';

import { expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

// Load dynamic backend port from .env
function getBackendPort(): number {
  // Check environment variable first
  if (process.env.BACKEND_PORT) {
    return parseInt(process.env.BACKEND_PORT);
  }

  // Load from .env file
  const envPath = path.resolve(__dirname, '../../../.env');
  if (fs.existsSync(envPath)) {
    const envContent = fs.readFileSync(envPath, 'utf8');
    const lines = envContent.split('\n');
    for (const line of lines) {
      const [key, value] = line.split('=');
      if (key === 'BACKEND_PORT') {
        return parseInt(value) || 8001;
      }
    }
  }

  return 8001; // Default fallback
}

export interface CreateAgentRequest {
  name?: string;
  model?: string;
  system_instructions?: string;
  task_instructions?: string;
  temperature?: number;
}

export interface Agent {
  id: string;
  name: string;
  model: string;
  system_instructions: string;
  task_instructions: string;
  temperature: number;
  created_at: string;
  updated_at: string;
}

export interface CreateThreadRequest {
  title?: string;
  agent_id: string;
}

export interface Thread {
  id: string;
  title: string;
  agent_id: string;
  created_at: string;
  updated_at: string;
}

export class ApiClient {
  private baseUrl: string;
  private headers: Record<string, string>;
  private workerId: string;

  constructor(workerId: string = '0', baseUrl?: string) {
    // Single backend port – per-worker DB isolation is via X-Test-Worker header
    const basePort = getBackendPort();
    this.baseUrl = baseUrl || `http://localhost:${basePort}`;
    this.workerId = workerId;
    this.headers = {
      'Content-Type': 'application/json',
      // CRITICAL: X-Test-Worker header routes requests to worker-specific Postgres schema
      // Without this, requests hit the default schema and can cross-contaminate workers
      // See: docs/work/e2e-test-infrastructure-redesign.md
      'X-Test-Worker': workerId,
    };
  }

  setAuthToken(token: string) {
    this.headers['Authorization'] = `Bearer ${token}`;
  }

  private async request(method: string, path: string, body?: any, retryCount = 0): Promise<any> {
    const url = `${this.baseUrl}${path}`;
    const MAX_RETRIES = 2;
    let response;
    let errorText = '';

    try {
      response = await fetch(url, {
        method,
        headers: this.headers,
        body: body ? JSON.stringify(body) : undefined,
      });

      if (!response.ok) {
        errorText = await response.text();
        throw new Error(`API request failed: ${method} ${path} - ${response.status} ${response.statusText}: ${errorText}`);
      }

      const contentType = response.headers.get('content-type');
      if (contentType && contentType.includes('application/json')) {
        return await response.json();
      }
      return await response.text();
    } catch (error) {
      // Only log on final attempt to avoid spam
      if (retryCount >= MAX_RETRIES) {
        testLog.error(`API request failed after ${retryCount + 1} attempts: ${method} ${path}`, {
          error: error instanceof Error ? error.message : String(error),
          responseStatus: response?.status,
        });
      }

      // Retry on 500 errors, but with a limit
      if (response?.status === 500 && retryCount < MAX_RETRIES) {
        await new Promise(resolve => setTimeout(resolve, 500));
        return this.request(method, path, body, retryCount + 1);
      }

      throw error;
    }
  }

  async createAgent(data: CreateAgentRequest = {}): Promise<Agent> {
    const agentData = {
      name: data.name || `Test Agent ${Date.now()}`,
      model: data.model || 'gpt-mock',  // Use test-friendly model
      system_instructions: data.system_instructions || 'You are a helpful AI assistant.',
      task_instructions: data.task_instructions || 'Please help the user with their request.',
      ...data
    };

    return await this.request('POST', '/api/agents', agentData);
  }

  async getAgent(id: string): Promise<Agent> {
    return await this.request('GET', `/api/agents/${id}`);
  }

  async updateAgent(id: string, data: Partial<CreateAgentRequest>): Promise<Agent> {
    return await this.request('PUT', `/api/agents/${id}`, data);
  }

  async deleteAgent(id: string): Promise<void> {
    await this.request('DELETE', `/api/agents/${id}`);
  }

  async listAgents(): Promise<Agent[]> {
    return await this.request('GET', '/api/agents');
  }

  async createThread(data: CreateThreadRequest): Promise<Thread> {
    const threadData = {
      title: data.title || `Test Thread ${Date.now()}`,
      ...data
    };

    return await this.request('POST', '/api/threads', threadData);
  }

  async getThread(id: string): Promise<Thread> {
    return await this.request('GET', `/api/threads/${id}`);
  }

  async deleteThread(id: string): Promise<void> {
    await this.request('DELETE', `/api/threads/${id}`);
  }

  async listThreads(agentId?: string): Promise<Thread[]> {
    const url = agentId ? `/api/threads?agent_id=${agentId}` : '/api/threads';
    return await this.request('GET', url);
  }

  async resetDatabase(): Promise<void> {
    try {
      await this.request('POST', '/api/admin/reset-database', { reset_type: 'clear_data' });
      // Wait for database to be fully reset before proceeding
      await new Promise(resolve => setTimeout(resolve, 1000));
    } catch (error) {
      testLog.error('Database reset failed, trying fallback cleanup...');
      // If reset fails, try to manually clean up test data
      const agents = await this.listAgents();
      await Promise.all(agents.map(agent => this.deleteAgent(agent.id)));
    }
  }

  async healthCheck(): Promise<any> {
    return await this.request('GET', '/');
  }
}

// Helper function to create an API client with the correct worker ID
export function createApiClient(workerId: string): ApiClient {
  return new ApiClient(workerId);
}
