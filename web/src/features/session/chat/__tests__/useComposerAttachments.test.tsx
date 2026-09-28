import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useComposerAttachments } from "../useComposerAttachments";

vi.mock("../imageCompression", () => ({
  compressImageForUpload: vi.fn(async (file: File) => ({
    blob: new Blob([new Uint8Array(file.size)], { type: "image/webp" }),
    mimeType: "image/webp",
    width: 100,
    height: 100,
    byteSize: file.size,
  })),
  ImageCompressionError: class extends Error {},
}));

function makeFile(name: string, type: string, size = 1024): File {
  return new File([new Uint8Array(size)], name, { type });
}

beforeEach(() => {
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: vi.fn(() => "blob://mock"),
    revokeObjectURL: vi.fn(),
  });
});

describe("useComposerAttachments", () => {
  it("accepts up to 4 valid images and surfaces an error past the cap", async () => {
    const { result } = renderHook(() => useComposerAttachments());
    await act(async () => {
      await result.current.addFiles([
        makeFile("a.png", "image/png"),
        makeFile("b.png", "image/png"),
        makeFile("c.png", "image/png"),
        makeFile("d.png", "image/png"),
      ]);
    });
    expect(result.current.attachments).toHaveLength(4);

    await act(async () => {
      await result.current.addFiles([makeFile("e.png", "image/png")]);
    });
    expect(result.current.attachments).toHaveLength(4);
    expect(result.current.error).toMatch(/max/);
  });

  it("rejects unsupported mime types", async () => {
    const { result } = renderHook(() => useComposerAttachments());
    await act(async () => {
      await result.current.addFiles([makeFile("x.svg", "image/svg+xml")]);
    });
    expect(result.current.attachments).toHaveLength(0);
    expect(result.current.error).toMatch(/unsupported/);
  });

  it("rejects items still over the byte cap after compression", async () => {
    const { compressImageForUpload } = await import("../imageCompression");
    (compressImageForUpload as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      blob: new Blob([new Uint8Array(3 * 1024 * 1024)], { type: "image/webp" }),
      mimeType: "image/webp",
      width: 4000,
      height: 4000,
      byteSize: 3 * 1024 * 1024,
    });
    const { result } = renderHook(() => useComposerAttachments());
    await act(async () => {
      await result.current.addFiles([makeFile("big.png", "image/png", 4 * 1024 * 1024)]);
    });
    expect(result.current.attachments).toHaveLength(0);
    expect(result.current.error).toMatch(/2 MB/);
  });

  it("removes one and clears all", async () => {
    const { result } = renderHook(() => useComposerAttachments());
    await act(async () => {
      await result.current.addFiles([
        makeFile("a.png", "image/png"),
        makeFile("b.png", "image/png"),
      ]);
    });
    const firstId = result.current.attachments[0].clientId;
    act(() => result.current.removeAttachment(firstId));
    expect(result.current.attachments).toHaveLength(1);

    act(() => result.current.clear());
    expect(result.current.attachments).toHaveLength(0);
    expect(URL.revokeObjectURL).toHaveBeenCalled();
  });
});
