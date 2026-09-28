/**
 * Resize + recompress an image attachment before upload.
 *
 * Browsers + iPhone screenshots routinely produce 4-8 MB PNGs that the
 * server would reject (2 MB cap). Compressing client-side keeps the
 * happy path under the cap without forcing a re-pick. We never change
 * orientation here; PHPicker / browser file inputs already deliver
 * upright bitmaps via `createImageBitmap`.
 */

const MAX_LONG_EDGE = 2048;
const WEBP_QUALITY = 0.85;
const JPEG_QUALITY = 0.85;

export interface CompressedImage {
  blob: Blob;
  mimeType: string;
  width: number;
  height: number;
  byteSize: number;
}

export class ImageCompressionError extends Error {}

export async function compressImageForUpload(file: File): Promise<CompressedImage> {
  if (!file.type.startsWith("image/")) {
    throw new ImageCompressionError(`unsupported file type: ${file.type || "unknown"}`);
  }

  const bitmap = await safeCreateImageBitmap(file);
  try {
    const { width, height } = scaleToFit(bitmap.width, bitmap.height, MAX_LONG_EDGE);
    const canvas = createCanvas(width, height);
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new ImageCompressionError("2d canvas context unavailable");
    ctx.drawImage(bitmap, 0, 0, width, height);

    const { blob, mimeType } = await encodeBest(canvas);
    return { blob, mimeType, width, height, byteSize: blob.size };
  } finally {
    bitmap.close?.();
  }
}

async function safeCreateImageBitmap(file: File): Promise<ImageBitmap> {
  try {
    return await createImageBitmap(file);
  } catch (err) {
    throw new ImageCompressionError(
      `could not decode image: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
}

function scaleToFit(w: number, h: number, maxEdge: number): { width: number; height: number } {
  const longest = Math.max(w, h);
  if (longest <= maxEdge) return { width: w, height: h };
  const ratio = maxEdge / longest;
  return { width: Math.round(w * ratio), height: Math.round(h * ratio) };
}

type AnyCanvas = HTMLCanvasElement | OffscreenCanvas;

function createCanvas(width: number, height: number): AnyCanvas {
  if (typeof OffscreenCanvas !== "undefined") {
    return new OffscreenCanvas(width, height);
  }
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  return canvas;
}

async function encodeBest(canvas: AnyCanvas): Promise<{ blob: Blob; mimeType: string }> {
  // Prefer webp — significantly smaller than png/jpeg at the same visual
  // quality. Some Safari builds don't encode webp from canvas; fall back
  // to jpeg in that case rather than raising the byte budget.
  const webp = await tryEncode(canvas, "image/webp", WEBP_QUALITY);
  if (webp) return { blob: webp, mimeType: "image/webp" };
  const jpeg = await tryEncode(canvas, "image/jpeg", JPEG_QUALITY);
  if (jpeg) return { blob: jpeg, mimeType: "image/jpeg" };
  throw new ImageCompressionError("canvas did not produce a blob");
}

async function tryEncode(canvas: AnyCanvas, mime: string, quality: number): Promise<Blob | null> {
  try {
    if (canvas instanceof OffscreenCanvas) {
      const blob = await canvas.convertToBlob({ type: mime, quality });
      return blob.type === mime ? blob : null;
    }
    return await new Promise<Blob | null>((resolve) =>
      (canvas as HTMLCanvasElement).toBlob(
        (b) => resolve(b && b.type === mime ? b : null),
        mime,
        quality,
      ),
    );
  } catch {
    return null;
  }
}
