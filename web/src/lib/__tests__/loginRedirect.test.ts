import { describe, expect, it } from "vitest";
import { DEFAULT_RETURN_TO, sanitizeReturnTo, buildLoginUrl } from "../loginRedirect";

describe("sanitizeReturnTo", () => {
  it("returns DEFAULT_RETURN_TO for null", () => {
    expect(sanitizeReturnTo(null)).toBe(DEFAULT_RETURN_TO);
  });

  it("returns DEFAULT_RETURN_TO for undefined", () => {
    expect(sanitizeReturnTo(undefined)).toBe(DEFAULT_RETURN_TO);
  });

  it("returns DEFAULT_RETURN_TO for empty string", () => {
    expect(sanitizeReturnTo("")).toBe(DEFAULT_RETURN_TO);
  });

  it("allows a plain path", () => {
    expect(sanitizeReturnTo("/sessions")).toBe("/sessions");
  });

  it("allows a path with search params", () => {
    expect(sanitizeReturnTo("/sessions?q=foo")).toBe("/sessions?q=foo");
  });

  it("allows a path with hash", () => {
    expect(sanitizeReturnTo("/timeline#top")).toBe("/timeline#top");
  });

  it("rejects a scheme-relative URL (// open-redirect)", () => {
    expect(sanitizeReturnTo("//evil.com")).toBe(DEFAULT_RETURN_TO);
  });

  it("rejects a backslash-prefixed path (IE open-redirect trick)", () => {
    expect(sanitizeReturnTo("/\\evil.com")).toBe(DEFAULT_RETURN_TO);
  });

  it("rejects a value that does not start with /", () => {
    expect(sanitizeReturnTo("evil.com/path")).toBe(DEFAULT_RETURN_TO);
  });

  it("rejects an absolute https URL", () => {
    expect(sanitizeReturnTo("https://evil.com/path")).toBe(DEFAULT_RETURN_TO);
  });

  it("rejects a javascript: URI", () => {
    expect(sanitizeReturnTo("javascript:alert(1)")).toBe(DEFAULT_RETURN_TO);
  });

  it("rejects a data: URI", () => {
    expect(sanitizeReturnTo("data:text/html,<b>hi</b>")).toBe(DEFAULT_RETURN_TO);
  });

  it("rejects URL-encoded double-slash (///evil.com after decode)", () => {
    // %2F%2Fevil.com — does not start with / so falls into the non-path branch
    expect(sanitizeReturnTo("%2F%2Fevil.com")).toBe(DEFAULT_RETURN_TO);
  });

  it("strips host/scheme from a full same-origin URL constructed via new URL()", () => {
    // new URL("/path", window.location.origin) → same origin, only path returned
    expect(sanitizeReturnTo("/sessions/abc-123")).toBe("/sessions/abc-123");
  });
});

describe("buildLoginUrl", () => {
  it("encodes the return_to path", () => {
    expect(buildLoginUrl("/sessions/abc-123")).toBe(
      `/login?return_to=${encodeURIComponent("/sessions/abc-123")}`,
    );
  });

  it("falls back to DEFAULT_RETURN_TO for unsafe input", () => {
    expect(buildLoginUrl("//evil.com")).toBe(
      `/login?return_to=${encodeURIComponent(DEFAULT_RETURN_TO)}`,
    );
  });
});
