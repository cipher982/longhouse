import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_RETURN_TO } from "../loginRedirect";
import { requestNativeAuth, supportsNativeAuthBridge } from "../nativeAuthBridge";

describe("nativeAuthBridge", () => {
  afterEach(() => {
    delete window.LonghouseNativeAuth;
  });

  it("returns false when the native auth bridge is unavailable", () => {
    expect(supportsNativeAuthBridge()).toBe(false);
    expect(requestNativeAuth("/timeline/abc")).toBe(false);
  });

  it("sends a sanitized return_to payload to the native auth bridge", () => {
    const requestAuth = vi.fn();
    window.LonghouseNativeAuth = { requestAuth };

    expect(supportsNativeAuthBridge()).toBe(true);
    expect(requestNativeAuth("//evil.com")).toBe(true);
    expect(requestAuth).toHaveBeenCalledWith({
      return_to: DEFAULT_RETURN_TO,
    });
  });
});
