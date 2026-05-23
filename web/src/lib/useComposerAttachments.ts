import { useCallback, useEffect, useRef, useState } from "react";

import { compressImageForUpload, ImageCompressionError } from "./imageCompression";

const MAX_ATTACHMENTS = 4;
const MAX_BYTES = 2 * 1024 * 1024;
const ALLOWED_MIME = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);

export interface ComposerAttachment {
  /** Stable client-side id for keyed React lists; not sent to the server. */
  clientId: string;
  filename: string;
  mimeType: string;
  byteSize: number;
  /** Object URL for thumbnail preview. Revoked when the attachment is dropped. */
  previewUrl: string;
  blob: Blob;
}

export interface UseComposerAttachmentsApi {
  attachments: ComposerAttachment[];
  addFiles: (files: FileList | File[]) => Promise<void>;
  removeAttachment: (clientId: string) => void;
  clear: () => void;
  isCompressing: boolean;
  error: string | null;
  clearError: () => void;
}

export function useComposerAttachments(): UseComposerAttachmentsApi {
  // Ref is the source of truth so concurrent compress operations agree on the
  // current count; state mirrors it for render. React 18's batching makes a
  // pure setState(prev=>...) callback unreliable as a "read latest" path.
  const ref = useRef<ComposerAttachment[]>([]);
  const [attachments, setAttachmentsState] = useState<ComposerAttachment[]>([]);
  const [isCompressing, setIsCompressing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const counterRef = useRef(0);
  const mountedRef = useRef(true);

  const sync = useCallback(() => {
    if (!mountedRef.current) return;
    setAttachmentsState([...ref.current]);
  }, []);

  useEffect(() => {
    return () => {
      mountedRef.current = false;
      ref.current.forEach((a) => URL.revokeObjectURL(a.previewUrl));
      ref.current = [];
    };
  }, []);

  const addFiles = useCallback(
    async (files: FileList | File[]) => {
      const incoming = Array.from(files);
      if (!incoming.length) return;
      setError(null);
      setIsCompressing(true);
      try {
        const slotsLeft = MAX_ATTACHMENTS - ref.current.length;
        if (slotsLeft <= 0) {
          setError(`max ${MAX_ATTACHMENTS} attachments`);
          return;
        }
        for (const file of incoming.slice(0, slotsLeft)) {
          if (!ALLOWED_MIME.has(file.type)) {
            setError(`unsupported type: ${file.type || file.name}`);
            continue;
          }
          try {
            const compressed = await compressImageForUpload(file);
            if (compressed.byteSize > MAX_BYTES) {
              setError(
                `${file.name} is still ${Math.round(compressed.byteSize / 1024)}KB after compression (max 2 MB)`,
              );
              continue;
            }
            counterRef.current += 1;
            ref.current.push({
              clientId: `att-${counterRef.current}`,
              filename: file.name,
              mimeType: compressed.mimeType,
              byteSize: compressed.byteSize,
              previewUrl: URL.createObjectURL(compressed.blob),
              blob: compressed.blob,
            });
          } catch (err) {
            if (err instanceof ImageCompressionError) {
              setError(err.message);
            } else {
              setError(err instanceof Error ? err.message : "could not process image");
            }
          }
        }
        // Cap defensively in case the loop somehow over-pushed; revoke any
        // overflow previews so we don't leak object URLs.
        if (ref.current.length > MAX_ATTACHMENTS) {
          const overflow = ref.current.slice(MAX_ATTACHMENTS);
          overflow.forEach((a) => URL.revokeObjectURL(a.previewUrl));
          ref.current = ref.current.slice(0, MAX_ATTACHMENTS);
        }
        sync();
      } finally {
        if (mountedRef.current) setIsCompressing(false);
      }
    },
    [sync],
  );

  const removeAttachment = useCallback(
    (clientId: string) => {
      const target = ref.current.find((a) => a.clientId === clientId);
      if (target) URL.revokeObjectURL(target.previewUrl);
      ref.current = ref.current.filter((a) => a.clientId !== clientId);
      sync();
    },
    [sync],
  );

  const clear = useCallback(() => {
    ref.current.forEach((a) => URL.revokeObjectURL(a.previewUrl));
    ref.current = [];
    sync();
    setError(null);
  }, [sync]);

  const clearError = useCallback(() => setError(null), []);

  return { attachments, addFiles, removeAttachment, clear, isCompressing, error, clearError };
}

export const COMPOSER_ATTACHMENT_LIMITS = {
  maxAttachments: MAX_ATTACHMENTS,
  maxBytes: MAX_BYTES,
  allowedMime: ALLOWED_MIME,
} as const;
