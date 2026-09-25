import { describe, expect, it } from "vitest";
import { ApiError } from "../../services/api/base";
import { shouldRetryQuery } from "../queryRetry";

const apiError = (status: number) => new ApiError({ url: "/api/x", status, body: null });

describe("shouldRetryQuery", () => {
  it("fails fast on permanent client errors", () => {
    for (const status of [400, 401, 403, 404, 409, 422]) {
      expect(shouldRetryQuery(0, apiError(status))).toBe(false);
    }
  });

  it("retries transient failures up to three times", () => {
    for (const error of [apiError(500), apiError(503), apiError(408), apiError(429), new TypeError("Failed to fetch")]) {
      expect(shouldRetryQuery(0, error)).toBe(true);
      expect(shouldRetryQuery(2, error)).toBe(true);
      expect(shouldRetryQuery(3, error)).toBe(false);
    }
  });
});
