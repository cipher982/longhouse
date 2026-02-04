// GENERATED CODE - DO NOT EDIT
// Generated from api-schema.yml

export interface ApiClientConfig {
  baseUrl: string;
  headers?: Record<string, string>;
}

export class ApiClient {
  private baseUrl: string;
  private headers: Record<string, string>;

  constructor(config: ApiClientConfig) {
    this.baseUrl = config.baseUrl;
    this.headers = config.headers || {};
  }

  private async fetch<T>(url: string, method: string, body?: unknown): Promise<T> {
    const response = await fetch(`${this.baseUrl}${url}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        ...this.headers,
      },
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status} ${response.statusText}`);
    }

    return response.json();
  }


  /** List fiches */
  async listFiches(): Promise<unknown> {
    return this.fetch(`/api/fiches`, 'GET');
  }

  /** Create fiche */
  async createFiche(): Promise<unknown> {
    return this.fetch(`/api/fiches`, 'POST');
  }

  /** Get fiche by ID */
  async getFicheById(): Promise<unknown> {
    return this.fetch(`/api/fiches/{fiche_id}`, 'GET');
  }

  /** Update fiche */
  async updateFiche(): Promise<unknown> {
    return this.fetch(`/api/fiches/{fiche_id}`, 'PUT');
  }

  /** Delete fiche */
  async deleteFiche(): Promise<unknown> {
    return this.fetch(`/api/fiches/{fiche_id}`, 'DELETE');
  }

  /** List threads */
  async listThreads(): Promise<unknown> {
    return this.fetch(`/api/threads`, 'GET');
  }

  /** Get thread messages */
  async getThreadMessages(): Promise<unknown> {
    return this.fetch(`/api/threads/{thread_id}/messages`, 'GET');
  }

  /** Create thread message */
  async createThreadMessage(): Promise<unknown> {
    return this.fetch(`/api/threads/{thread_id}/messages`, 'POST');
  }
}
