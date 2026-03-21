import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { clearApiError, reportApiError, useApiHealth } from "../apiHealth";

describe("useApiHealth", () => {
  afterEach(() => {
    clearApiError();
  });

  it("reads the current snapshot and reacts to store updates", () => {
    const initialError = new Error("backend unavailable");
    reportApiError(initialError);

    const { result } = renderHook(() => useApiHealth());
    expect(result.current).toBe(initialError);

    const nextError = new Error("gateway timeout");
    act(() => {
      reportApiError(nextError);
    });
    expect(result.current).toBe(nextError);

    act(() => {
      clearApiError();
    });
    expect(result.current).toBeNull();
  });
});
