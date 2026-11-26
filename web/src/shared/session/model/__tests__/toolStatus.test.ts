import { describe, expect, it } from "vitest";
import { toolStatusPhrase } from "../toolStatus";

describe("toolStatusPhrase", () => {
  it("uses the canonical phrase for a known tool in any provider's spelling", () => {
    expect(toolStatusPhrase("Bash")).toBe("Running a command");
    expect(toolStatusPhrase("exec_command")).toBe("Running a command");
    expect(toolStatusPhrase("bash")).toBe("Running a command");
    expect(toolStatusPhrase("Grep")).toBe("Searching");
  });
  it("reads a command line or a name with JSON arguments without leaking them", () => {
    expect(toolStatusPhrase("sleep 6")).toBe("Running a command");
    expect(toolStatusPhrase('view_file {"AbsolutePath": "/x"}')).toBe(toolStatusPhrase("view_file"));
    expect(toolStatusPhrase('view_file {"AbsolutePath": "/x"}')).not.toContain("{");
  });
  it("names an MCP tool by its method", () => {
    expect(toolStatusPhrase("mcp__trove__search_personal_memory")).toBe("Using search personal memory");
  });
});
