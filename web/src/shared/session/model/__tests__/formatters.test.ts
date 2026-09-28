import { describe, expect, it } from "vitest";
import { formatTurnDuration } from "../formatters";

describe("formatTurnDuration", () => {
  it("compacts a turn duration the way the provider terminal does", () => {
    expect(formatTurnDuration(129_299)).toBe("2m 9s");
    expect(formatTurnDuration(58_459)).toBe("58s");
    expect(formatTurnDuration(49)).toBe("0s");
    expect(formatTurnDuration(780_000)).toBe("13m");
    expect(formatTurnDuration(3_720_000)).toBe("1h 2m");
    expect(formatTurnDuration(7_200_000)).toBe("2h");
  });
});
