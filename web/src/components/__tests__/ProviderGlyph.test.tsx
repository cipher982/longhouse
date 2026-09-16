import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ProviderGlyph } from "../ProviderGlyph";

const SUPPORTED_PROVIDERS = [
  "claude",
  "codex",
  "cursor",
  "opencode",
  "pi",
  "omp",
  "antigravity",
] as const;

function hasFallbackMark(container: HTMLElement): boolean {
  return Array.from(container.querySelectorAll("path")).some((path) =>
    path.getAttribute("d")?.startsWith("m8 9 3 3-3 3"),
  );
}

describe("ProviderGlyph", () => {
  it.each(SUPPORTED_PROVIDERS)("renders a branded mark for %s", (provider) => {
    const { container } = render(<ProviderGlyph provider={provider} />);

    expect(screen.getByRole("img")).toBeInTheDocument();
    expect(hasFallbackMark(container)).toBe(false);
  });

  it("keeps the z.ai archive alias branded", () => {
    const { container } = render(<ProviderGlyph provider="z.ai" />);

    expect(screen.getByRole("img", { name: "Z.ai" })).toBeInTheDocument();
    expect(hasFallbackMark(container)).toBe(false);
  });
  it("trims provider keys before lookup", () => {
    const { container } = render(<ProviderGlyph provider=" omp " />);

    expect(screen.getByRole("img", { name: "OMP" })).toBeInTheDocument();
    expect(hasFallbackMark(container)).toBe(false);
  });

  it("keeps the OMP connector solid in monochrome mode", () => {
    const { container } = render(<ProviderGlyph provider="omp" tone="mono" />);

    expect(
      Array.from(container.querySelectorAll("rect")).some(
        (rect) => rect.getAttribute("fill") === "#0D0D0D",
      ),
    ).toBe(false);
  });
});
