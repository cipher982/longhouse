import { describe, expect, it } from "vitest";
import {
  getLaunchProviderSupport,
  getLaunchProviderSupportList,
} from "../providers";

describe("providers launch support", () => {
  it("keeps the launch provider capability contract explicit", () => {
    const providers = getLaunchProviderSupportList();
    expect(providers.map((provider) => provider.id)).toEqual([
      "claude",
      "codex",
      "cursor",
      "opencode",
      "antigravity",
    ]);
    expect(providers.every((provider) => provider.archiveVisibility === "live")).toBe(true);
    expect(providers.every((provider) => provider.cloudSessionStart === "live")).toBe(true);
    expect(providers.every((provider) => provider.launchAndSend)).toBe(true);
  });

  it("mirrors the managed provider contract capability matrix", () => {
    expect(getLaunchProviderSupport("claude")).toMatchObject({
      interrupt: true,
      steerMidTurn: true,
      resume: true,
    });
    expect(getLaunchProviderSupport("codex")).toMatchObject({
      interrupt: true,
      steerMidTurn: true,
      resume: true,
    });
    expect(getLaunchProviderSupport("cursor")).toMatchObject({
      interrupt: true,
      steerMidTurn: false,
      resume: true,
    });
    expect(getLaunchProviderSupport("opencode")).toMatchObject({
      interrupt: true,
      steerMidTurn: false,
      resume: false,
    });
    expect(getLaunchProviderSupport("antigravity")).toMatchObject({
      interrupt: false,
      steerMidTurn: false,
      resume: false,
    });
  });

  it("records hooks and telemetry differences for the landing truth pass", () => {
    expect(getLaunchProviderSupport("claude")).toMatchObject({
      hooksSupport: "live",
      telemetryQuality: "rich",
    });
    expect(getLaunchProviderSupport("codex")).toMatchObject({
      hooksSupport: "none",
      telemetryQuality: "structured",
    });
    expect(getLaunchProviderSupport("antigravity")).toMatchObject({
      hooksSupport: "live",
      telemetryQuality: "structured",
    });
  });
});
