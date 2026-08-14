import { describe, expect, it } from "vitest";
import { advanceDemoClock } from "../useDemoClock";

describe("demo clock cycles", () => {
  it("advances the cycle only when playback wraps", () => {
    expect(advanceDemoClock(9.8, 2, 0.3, 10)).toEqual({ positionSec: 0.10000000000000142, cycle: 3 });
    expect(advanceDemoClock(3, 2, 0.25, 10)).toEqual({ positionSec: 3.25, cycle: 2 });
  });

  it("counts multiple wraps without coupling them to seek behavior", () => {
    expect(advanceDemoClock(2, 4, 28, 10)).toEqual({ positionSec: 0, cycle: 7 });
  });
});
