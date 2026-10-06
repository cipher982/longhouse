import { ApiError } from "@/shared/api/base";

/**
 * One line of technical detail for an error screen's collapsed "Details":
 * HTTP status, the server's machine-readable code, and its message. Error
 * screens lead with plain copy; this is what a bug report needs.
 */
export function errorDetails(error: unknown): string | undefined {
  if (!error) return undefined;
  if (error instanceof ApiError) {
    const detail =
      error.body && typeof error.body === "object" && "detail" in error.body
        ? (error.body as { detail?: unknown }).detail
        : undefined;
    const code =
      detail && typeof detail === "object" && "code" in detail && typeof detail.code === "string"
        ? detail.code
        : undefined;
    return [`HTTP ${error.status}`, code, error.message].filter(Boolean).join(" · ");
  }
  if (error instanceof Error) return error.message || error.name;
  return String(error);
}
