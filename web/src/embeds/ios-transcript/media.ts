import { escapeHtml } from "./escape";
import type { MediaRef } from "./types";

export function mediaStrip(media: MediaRef[] | null | undefined): string {
  const items = (media || [])
    .map((ref) => {
      if (!ref || !ref.url) {
        const label = ref && ref.mediaState === "pending" ? "Media pending" : "Media unavailable";
        return `<span class="media-placeholder">${escapeHtml(label)}</span>`;
      }
      const href = ref.blobUrl || ref.url;
      const alt = "Session media " + String(ref.sha256 || "").slice(0, 12);
      const hasDimensions =
        Number.isInteger(ref.width) && (ref.width as number) > 0 && Number.isInteger(ref.height) && (ref.height as number) > 0;
      const dimensions = hasDimensions ? ` width="${ref.width}" height="${ref.height}"` : "";
      const animated = String(ref.mimeType || "")
        .toLowerCase()
        .startsWith("image/gif")
        ? '<span class="media-animated">Animated still</span>'
        : "";
      return `
          <a class="media-item" href="${escapeHtml(href)}" target="_blank" rel="noreferrer noopener">
            <img src="${escapeHtml(ref.url)}" alt="${escapeHtml(alt)}" loading="lazy"${dimensions} onerror="this.outerHTML='&lt;span class=&quot;media-placeholder&quot;&gt;Media unavailable&lt;/span&gt;'">
            ${animated}
          </a>
        `;
    })
    .join("");
  return items ? `<div class="media-strip">${items}</div>` : "";
}
