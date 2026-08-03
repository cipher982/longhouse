import { beforeEach, describe, expect, it } from "vitest";

import {
  getClockCalibration,
  recordClockSyncSample,
  resetClockCalibrationForTests,
} from "../renderBeacon";

describe("render beacon clock calibration", () => {
  beforeEach(() => resetClockCalibrationForTests());

  it("derives client skew and network RTT from four timestamps", () => {
    recordClockSyncSample({
      clientSentAtMs: 1_000,
      serverReceivedAtMs: 985,
      serverSentAtMs: 987,
      clientReceivedAtMs: 1_072,
    });

    expect(getClockCalibration()).toEqual({
      clockSkewMs: 50,
      rttMs: 70,
      uncertaintyMs: 35,
      sampleCount: 1,
    });
  });

  it("keeps the lowest-RTT sample while retaining sample count", () => {
    recordClockSyncSample({
      clientSentAtMs: 1_000,
      serverReceivedAtMs: 985,
      serverSentAtMs: 987,
      clientReceivedAtMs: 1_072,
    });
    recordClockSyncSample({
      clientSentAtMs: 2_000,
      serverReceivedAtMs: 1_985,
      serverSentAtMs: 1_987,
      clientReceivedAtMs: 2_042,
    });

    expect(getClockCalibration()).toEqual({
      clockSkewMs: 35,
      rttMs: 40,
      uncertaintyMs: 20,
      sampleCount: 2,
    });
  });
});
