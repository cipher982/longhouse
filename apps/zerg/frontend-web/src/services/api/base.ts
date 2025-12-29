import { config } from "../../lib/config";
import { logger } from "../../jarvis/core/logger";

export class ApiError extends Error {
  readonly status: number;
  readonly url: string;
  readonly body: unknown;

  constructor({ url, status, body }: { url: string; status: number; body: unknown }) {
    let detailMessage = `Request to ${url} failed with status ${status}`;
    if (body && typeof body === 'object' && 'detail' in body) {
      detailMessage = `${detailMessage}: ${body.detail}`;
    }

    super(detailMessage);
    this.name = "ApiError";
    this.status = status;
    this.url = url;
    this.body = body;

    logger.error(`[API] ${detailMessage}`, { url, status, body });
  }
}

export function buildUrl(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const base = config.apiBaseUrl.replace(/\/+$/, "");

  if (base.startsWith("http")) {
    return `${base}${normalizedPath}`;
  }

  const prefix = base || "/api";
  return `${prefix}${normalizedPath}`;
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = buildUrl(path);
  const headers = new Headers(init?.headers);

  if (!headers.has("Content-Type") && init?.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const testWorkerHeader = typeof window !== "undefined" ? window.__TEST_WORKER_ID__ : undefined;
  if (testWorkerHeader !== undefined) {
    headers.set("X-Test-Worker", String(testWorkerHeader));
  }

  const response = await fetch(url, {
    ...init,
    headers,
    credentials: 'include',
  });

  const hasBody = response.status !== 204 && response.status !== 205;
  const contentType = response.headers.get("content-type") ?? "";
  const expectsJson = contentType.includes("application/json");
  let data: unknown = undefined;

  if (hasBody) {
    try {
      if (expectsJson) {
        data = await response.json();
      } else {
        const text = await response.text();
        data = text.length > 0 ? text : undefined;
      }
    } catch (error) {
      if (!response.ok) {
        throw new ApiError({ url, status: response.status, body: data });
      }
      throw error instanceof Error ? error : new Error("Failed to parse response body");
    }
  }

  if (!response.ok) {
    throw new ApiError({ url, status: response.status, body: data });
  }

  return data as T;
}
