import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Sparkline } from "../Sparkline";

describe("Sparkline", () => {
  it("renders a polyline when there are at least two non-empty buckets", () => {
    const { container } = render(<Sparkline data={[0, 1, 0, 2, 0]} />);
    expect(container.querySelector("svg")).toBeInTheDocument();
    expect(container.querySelector("polyline")).toBeInTheDocument();
  });

  it("renders nothing (not an empty box) with fewer than two non-empty buckets", () => {
    const { container } = render(<Sparkline data={[0, 0, 3, 0, 0]} />);
    expect(container.querySelector("svg")).not.toBeInTheDocument();
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing for an all-zero window", () => {
    const { container } = render(<Sparkline data={[0, 0, 0, 0]} />);
    expect(container.firstChild).toBeNull();
  });

  it("draws the live dot only when live", () => {
    const { container: liveContainer } = render(<Sparkline data={[1, 2, 3]} live />);
    expect(liveContainer.querySelector("circle")).toBeInTheDocument();

    const { container: staticContainer } = render(<Sparkline data={[1, 2, 3]} live={false} />);
    expect(staticContainer.querySelector("circle")).not.toBeInTheDocument();
  });
});
