import { describe, expect, it } from "vitest";
import {
  getLaunchProviderSupport,
  getLaunchProviderSupportList,
} from "../providers";

describe("providers launch support", () => {
  it("keeps the launch provider capability contract explicit", () => {
    const providers = getLaunchProviderSupportList();
    expect(providers.map((provider) => provider.id)).toEqual(["claude", "codex", "antigravity", "opencode"]);
    expect(providers.every((provider) => provider.archiveVisibility === "live")).toBe(true);
    expect(providers.every((provider) => provider.cloudSessionStart === "live")).toBe(true);
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
    expect(getLaunchProviderSupport("opencode")).toMatchObject({
      hooksSupport: "none",
      telemetryQuality: "structured",
    });
  });
});
