/**
 * Clipboard helper used by the session detail "Copy link" button and any other
 * surface that needs to put a short string in the user's clipboard.
 *
 * Mirrors the pattern in `pages/docs/CodeBlock.tsx` and
 * `components/landing/HeroSection.tsx` — try the modern Clipboard API first,
 * then fall back to a hidden-textarea + `document.execCommand("copy")` for
 * restricted contexts (older browsers, non-secure origins, some embedders).
 *
 * Returns true on success so the caller can decide whether to show a toast.
 */

export async function copyToClipboard(text: string): Promise<boolean> {
  if (typeof text !== "string" || text.length === 0) {
    return false;
  }

  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to the legacy path for restricted clipboard contexts.
    }
  }

  if (typeof document === "undefined") {
    return false;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  try {
    return document.execCommand("copy");
  } catch {
    // execCommand can throw in hardened contexts (e.g. some embedded
    // webviews). Treat as failure and let the caller surface an error.
    return false;
  } finally {
    document.body.removeChild(textarea);
  }
}

export function buildSessionShareUrl(baseUrl: string, shareUrlOrToken: string): string {
  const cleanBase = baseUrl.replace(/\/+$/, "");
  const raw = shareUrlOrToken.trim();
  if (/^https?:\/\//i.test(raw)) {
    return raw;
  }
  const path = raw.startsWith("/") ? raw : `/share/${encodeURIComponent(raw)}`;
  return `${cleanBase}${path}`;
}
