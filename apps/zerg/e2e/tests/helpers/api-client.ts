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

export interface CreateFicheRequest {
  name?: string;
  model?: string;
  system_instructions?: string;
  task_instructions?: string;
  temperature?: number;
}

export interface Fiche {
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
  fiche_id: string;
}

export interface Thread {
  id: string;
  title: string;
  fiche_id: string;
  created_at: string;
  updated_at: string;
}

export class ApiClient {
  private baseUrl: string;
  private headers: Record<string, string>;
  private commisId: string;

  constructor(commisId: string = '0', baseUrl?: string) {
    // Single backend port – per-commis DB isolation is via X-Test-Commis header
    const basePort = getBackendPort();
    this.baseUrl = baseUrl || `http://localhost:${basePort}`;
    this.commisId = commisId;
    this.headers = {
      'Content-Type': 'application/json',
      // CRITICAL: X-Test-Commis header routes requests to commis-specific Postgres schema
      // Without this, requests hit the default schema and can cross-contaminate commis
      // See: docs/work/e2e-test-infrastructure-redesign.md
      'X-Test-Commis': commisId,
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

  async createFiche(data: CreateFicheRequest = {}): Promise<Fiche> {
    const ficheData = {
      name: data.name || `Test Fiche ${Date.now()}`,
      model: data.model || 'gpt-mock',  // Use test-friendly model
      system_instructions: data.system_instructions || 'You are a helpful AI assistant.',
      task_instructions: data.task_instructions || 'Please help the user with their request.',
      ...data
    };

    return await this.request('POST', '/api/fiches', ficheData);
  }

  async getFiche(id: string): Promise<Fiche> {
    return await this.request('GET', `/api/fiches/${id}`);
  }

  async updateFiche(id: string, data: Partial<CreateFicheRequest>): Promise<Fiche> {
    return await this.request('PUT', `/api/fiches/${id}`, data);
  }

  async deleteFiche(id: string): Promise<void> {
    await this.request('DELETE', `/api/fiches/${id}`);
  }

  async listFiches(): Promise<Fiche[]> {
    return await this.request('GET', '/api/fiches');
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

  async listThreads(ficheId?: string): Promise<Thread[]> {
    const url = ficheId ? `/api/threads?fiche_id=${ficheId}` : '/api/threads';
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
      const fiches = await this.listFiches();
      await Promise.all(fiches.map(fiche => this.deleteFiche(fiche.id)));
    }
  }

  async healthCheck(): Promise<any> {
    return await this.request('GET', '/');
  }
}

// Helper function to create an API client with the correct commis ID
export function createApiClient(commisId: string): ApiClient {
  return new ApiClient(commisId);
}
