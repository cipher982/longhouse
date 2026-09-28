import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AppScreenshotFrame } from "../AppScreenshotFrame";

describe("AppScreenshotFrame", () => {
  it("shows an image that finished loading before React attached its handlers", () => {
    // A cached image reports complete before any load event reaches React.
    const complete = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "complete");
    const naturalWidth = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "naturalWidth");
    Object.defineProperty(HTMLImageElement.prototype, "complete", { configurable: true, get: () => true });
    Object.defineProperty(HTMLImageElement.prototype, "naturalWidth", { configurable: true, get: () => 2304 });
    try {
      render(<AppScreenshotFrame src="/shot.webp" alt="Timeline" title="Timeline" />);
      expect(screen.getByAltText("Timeline")).toHaveStyle({ opacity: "1" });
    } finally {
      if (complete) Object.defineProperty(HTMLImageElement.prototype, "complete", complete);
      if (naturalWidth) Object.defineProperty(HTMLImageElement.prototype, "naturalWidth", naturalWidth);
    }
  });
});
