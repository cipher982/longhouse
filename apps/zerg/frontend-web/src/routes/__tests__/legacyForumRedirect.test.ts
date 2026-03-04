import { describe, expect, it } from "vitest";
import { resolveLegacyForumRedirect } from "../App";

describe("resolveLegacyForumRedirect", () => {
  it("redirects /forum to /timeline when no session query exists", () => {
    expect(resolveLegacyForumRedirect("")).toEqual({ pathname: "/timeline" });
  });

  it("redirects session links to session detail without resume by default", () => {
    expect(resolveLegacyForumRedirect("?session=abc-123")).toEqual({
      pathname: "/timeline/abc-123",
      search: "",
    });
  });

  it("maps legacy chat=true query to resume mode", () => {
    expect(resolveLegacyForumRedirect("?session=abc-123&chat=true")).toEqual({
      pathname: "/timeline/abc-123",
      search: "?resume=1",
    });
  });
});
