import { describe, expect, it } from "vitest";
import {
  getLaunchProviderSupport,
  getLaunchProviderSupportList,
  supportsDirectWebContinuation,
} from "../providers";

describe("providers launch support", () => {
  it("keeps the launch provider capability contract explicit", () => {
    const providers = getLaunchProviderSupportList();
    expect(providers.map((provider) => provider.id)).toEqual(["claude", "codex", "gemini"]);
    expect(providers.every((provider) => provider.archiveVisibility === "live")).toBe(true);
    expect(providers.every((provider) => provider.cloudSessionStart === "live")).toBe(true);
  });

  it("marks Claude as the only direct web continuation provider today", () => {
    expect(supportsDirectWebContinuation("claude")).toBe(true);
    expect(supportsDirectWebContinuation("codex")).toBe(false);
    expect(supportsDirectWebContinuation("gemini")).toBe(false);
  });

  it("records hooks and telemetry differences for the landing truth pass", () => {
    expect(getLaunchProviderSupport("claude")).toMatchObject({
      hooksSupport: "live",
      telemetryQuality: "rich",
      directWebContinuation: "live",
    });
    expect(getLaunchProviderSupport("codex")).toMatchObject({
      hooksSupport: "none",
      telemetryQuality: "structured",
      directWebContinuation: "later",
    });
    expect(getLaunchProviderSupport("gemini")).toMatchObject({
      hooksSupport: "none",
      telemetryQuality: "basic",
      directWebContinuation: "later",
    });
  });
});
