import { describe, expect, it, vi, beforeEach } from "vitest";

import {
  compressImageForUpload,
  ImageCompressionError,
} from "../imageCompression";

class FakeImageBitmap {
  width: number;
  height: number;
  closed = false;
  constructor(width: number, height: number) {
    this.width = width;
    this.height = height;
  }
  close() {
    this.closed = true;
  }
}

let lastCanvas: { width: number; height: number } | null = null;

function installFakeCanvas(emit: { mime: string; bytes: number }) {
  lastCanvas = null;
  // jsdom doesn't ship canvas; install a minimal fake that records dimensions
  // and produces blobs of the requested mime + size.
  vi.stubGlobal("OffscreenCanvas", class {
    width: number;
    height: number;
    constructor(w: number, h: number) {
      this.width = w;
      this.height = h;
      lastCanvas = { width: w, height: h };
    }
    getContext() {
      return { drawImage: vi.fn() };
    }
    convertToBlob(opts?: { type?: string }) {
      const mime = opts?.type ?? "image/png";
      if (mime !== emit.mime) {
        return Promise.reject(new Error("unsupported mime"));
      }
      return Promise.resolve(new Blob([new Uint8Array(emit.bytes)], { type: mime }));
    }
  });
}

describe("compressImageForUpload", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn(async (file: File) => {
        if (file.name === "broken.png") throw new Error("decode failed");
        return new FakeImageBitmap(file.name === "huge.png" ? 4096 : 1024, file.name === "huge.png" ? 3072 : 768);
      }),
    );
  });

  it("rejects non-image inputs without calling decode", async () => {
    const file = new File(["x"], "note.txt", { type: "text/plain" });
    await expect(compressImageForUpload(file)).rejects.toBeInstanceOf(ImageCompressionError);
  });

  it("encodes webp when the browser supports it", async () => {
    installFakeCanvas({ mime: "image/webp", bytes: 1024 });
    const file = new File([new Uint8Array(2048)], "ok.png", { type: "image/png" });
    const out = await compressImageForUpload(file);
    expect(out.mimeType).toBe("image/webp");
    expect(out.byteSize).toBe(1024);
  });

  it("scales the longest edge to 2048", async () => {
    installFakeCanvas({ mime: "image/webp", bytes: 1024 });
    const file = new File([new Uint8Array(8192)], "huge.png", { type: "image/png" });
    await compressImageForUpload(file);
    expect(lastCanvas).toEqual({ width: 2048, height: 1536 });
  });

  it("falls back to jpeg when webp encoding is unavailable", async () => {
    installFakeCanvas({ mime: "image/jpeg", bytes: 4096 });
    const file = new File([new Uint8Array(2048)], "ok.png", { type: "image/png" });
    const out = await compressImageForUpload(file);
    expect(out.mimeType).toBe("image/jpeg");
  });

  it("surfaces decode failures as ImageCompressionError", async () => {
    installFakeCanvas({ mime: "image/webp", bytes: 1024 });
    const file = new File([new Uint8Array(2048)], "broken.png", { type: "image/png" });
    await expect(compressImageForUpload(file)).rejects.toBeInstanceOf(ImageCompressionError);
  });
});
