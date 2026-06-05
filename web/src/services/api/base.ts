import { config } from "../../lib/config";
import { fetchWithRefresh } from "../../lib/auth-refresh";
import { logger } from "../../lib/logger";

export const DEMO_READ_ONLY_MESSAGE =
  "This is a demo. You can browse sessions, but changes and session control are disabled.";

export function isDemoReadOnlyBody(body: unknown): body is { error?: string; demo?: boolean } {
  return Boolean(
    body
      && typeof body === "object"
      && "demo" in body
      && (body as { demo?: unknown }).demo === true,
  );
}

export class ApiError extends Error {
  readonly status: number;
  readonly url: string;
  readonly body: unknown;

  constructor({ url, status, body }: { url: string; status: number; body: unknown }) {
    const detailMessage = formatApiErrorMessage(status, body);
    super(detailMessage);
    this.name = "ApiError";
    this.status = status;
    this.url = url;
    this.body = body;

    logger.error(`[API] ${detailMessage}`, { url, status, body });
  }
}

function formatApiErrorMessage(status: number, body: unknown): string {
  let detailMessage = `Request failed (${status})`;
  if (isDemoReadOnlyBody(body)) {
    detailMessage = DEMO_READ_ONLY_MESSAGE;
  } else if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") {
      detailMessage = detail;
    } else if (Array.isArray(detail)) {
      const validationMessages = detail
        .map(formatValidationDetail)
        .filter((message): message is string => Boolean(message));
      if (validationMessages.length) {
        detailMessage = validationMessages.join("; ");
      }
    } else if (
      detail
      && typeof detail === "object"
      && "message" in detail
      && typeof detail.message === "string"
    ) {
      detailMessage = detail.message;
    }
  } else if (body && typeof body === "object" && "error" in body && typeof body.error === "string") {
    detailMessage = body.error;
  }

  return detailMessage;
}

function formatValidationDetail(detail: unknown): string | null {
  if (!detail || typeof detail !== "object") return null;
  const item = detail as { loc?: unknown; msg?: unknown; message?: unknown };
  const message =
    typeof item.msg === "string"
      ? item.msg
      : typeof item.message === "string"
        ? item.message
        : null;
  if (!message) return null;

  if (!Array.isArray(item.loc)) return message;
  const path = item.loc
    .map((part) => String(part))
    .filter((part) => part !== "body" && part !== "query" && part !== "path")
    .join(".");
  return path ? `${path}: ${message}` : message;
}

export function buildUrl(path: string): string {
  let normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const base = config.apiBaseUrl.replace(/\/+$/, "");
  const prefix = base || "/api";

  // Guard against double prefix (e.g. passing "/api/foo" when prefix is "/api")
  if (prefix && normalizedPath.startsWith(`${prefix}/`)) {
    normalizedPath = normalizedPath.slice(prefix.length);
  }

  if (base.startsWith("http")) {
    return `${base}${normalizedPath}`;
  }

  return `${prefix}${normalizedPath}`;
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = buildUrl(path);
  const headers = new Headers(init?.headers);

  if (!headers.has("Content-Type") && init?.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const testCommisHeader = typeof window !== "undefined" ? window.__TEST_COMMIS_ID__ : undefined;
  if (testCommisHeader !== undefined) {
    headers.set("X-Test-Commis", String(testCommisHeader));
  }

  const response = await fetchWithRefresh(url, {
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
