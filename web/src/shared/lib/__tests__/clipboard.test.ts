import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { copyToClipboard } from "../clipboard";

describe("copyToClipboard", () => {
  let originalClipboard: PropertyDescriptor | undefined;
  let originalExecCommand: typeof document.execCommand | undefined;

  beforeEach(() => {
    originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
    originalExecCommand = document.execCommand;
  });

  afterEach(() => {
    if (originalClipboard) {
      Object.defineProperty(navigator, "clipboard", originalClipboard);
    } else {
      // @ts-expect-error — restore the test runner's default
      delete navigator.clipboard;
    }
    document.execCommand = originalExecCommand as typeof document.execCommand;
  });

  it("uses the modern Clipboard API when available", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    document.execCommand = vi.fn(() => true) as typeof document.execCommand;

    const ok = await copyToClipboard("hello");

    expect(ok).toBe(true);
    expect(writeText).toHaveBeenCalledWith("hello");
    expect(document.execCommand).not.toHaveBeenCalled();
  });

  it("falls back to execCommand when the modern API throws", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const execCommand = vi.fn(() => true);
    document.execCommand = execCommand as typeof document.execCommand;

    const ok = await copyToClipboard("fallback value");

    expect(ok).toBe(true);
    expect(writeText).toHaveBeenCalledWith("fallback value");
    expect(execCommand).toHaveBeenCalledWith("copy");
  });

  it("falls back to execCommand when the modern API is missing", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    const execCommand = vi.fn(() => true);
    document.execCommand = execCommand as typeof document.execCommand;

    const ok = await copyToClipboard("legacy path");

    expect(ok).toBe(true);
    expect(execCommand).toHaveBeenCalledWith("copy");
  });

  it("returns false for empty input", async () => {
    const writeText = vi.fn();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    expect(await copyToClipboard("")).toBe(false);
    expect(writeText).not.toHaveBeenCalled();
  });
});
