import { describe, expect, it } from "vitest";
import { resolveToolInfo } from "../toolTiers.generated";

describe("exact tool aliases", () => {
  it("always translates known native names", () => {
    expect(resolveToolInfo("view_file").label).toBe("Read");
  });

  it("keeps unknown provider tools raw", () => {
    expect(resolveToolInfo("CallDynamicTool").label).toBe("CallDynamicTool");
  });
});
