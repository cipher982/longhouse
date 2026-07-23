import { describe, expect, it } from "vitest";
import { resolveToolInfo } from "../toolTiers.generated";

describe("exact tool aliases", () => {
  it("remain raw by default and translate only when dogfood is enabled", () => {
    expect(resolveToolInfo("view_file", false).label).toBe("view_file");
    expect(resolveToolInfo("view_file", true).label).toBe("Read");
  });

  it("keeps unknown provider tools raw even when dogfood is enabled", () => {
    expect(resolveToolInfo("CallDynamicTool", true).label).toBe("CallDynamicTool");
  });
});
