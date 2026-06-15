import { describe, expect, it } from "vitest";
import {
  DEFAULT_RETURN_TO,
  sanitizeReturnTo,
  buildLoginUrl,
  extractTimelineSessionId,
  shortSessionPrefix,
} from "../loginRedirect";

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

  it("preserves a nested path", () => {
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

describe("extractTimelineSessionId", () => {
  const valid = "111a5a5d-9c4e-4f3a-b7d2-0e8a3f5b1c2d";

  it("returns the UUID from a /timeline/<uuid> returnTo", () => {
    expect(extractTimelineSessionId(`/timeline/${valid}`)).toBe(valid);
  });

  it("accepts a trailing slash", () => {
    expect(extractTimelineSessionId(`/timeline/${valid}/`)).toBe(valid);
  });

  it("ignores search params and hash", () => {
    expect(extractTimelineSessionId(`/timeline/${valid}?view=compact#notes`)).toBe(valid);
  });

  it("lower-cases the returned id", () => {
    expect(extractTimelineSessionId(`/timeline/${valid.toUpperCase()}`)).toBe(valid);
  });

  it("returns null for a non-timeline path", () => {
    expect(extractTimelineSessionId("/timeline")).toBeNull();
    expect(extractTimelineSessionId("/sessions/abc-123")).toBeNull();
    expect(extractTimelineSessionId("/")).toBeNull();
  });

  it("returns null for a malformed UUID", () => {
    expect(extractTimelineSessionId("/timeline/not-a-uuid")).toBeNull();
    expect(extractTimelineSessionId("/timeline/111a5a5d-9c4e-4f3a")).toBeNull();
  });

  it("returns null for empty or unsafe input", () => {
    expect(extractTimelineSessionId("")).toBeNull();
    expect(extractTimelineSessionId("evil.com/timeline/abc")).toBeNull();
    expect(extractTimelineSessionId("//timeline/abc")).toBeNull();
  });
});

describe("shortSessionPrefix", () => {
  const valid = "111a5a5d-9c4e-4f3a-b7d2-0e8a3f5b1c2d";

  it("returns the 8-hex-char prefix", () => {
    expect(shortSessionPrefix(valid)).toBe("111a5a5d");
  });

  it("lower-cases the prefix", () => {
    expect(shortSessionPrefix(valid.toUpperCase())).toBe("111a5a5d");
  });

  it("returns null for an empty id", () => {
    expect(shortSessionPrefix("")).toBeNull();
  });

  it("returns null when the head is not 8 hex chars", () => {
    expect(shortSessionPrefix("xyz-not-hex-rest")).toBeNull();
    expect(shortSessionPrefix("111a5a-9c4e-4f3a-b7d2-0e8a3f5b1c2d")).toBeNull();
  });
});
