import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Nixie } from "../Nixie";

describe("Nixie", () => {
  it("renders the value", () => {
    render(<Nixie value="35:37" />);
    expect(screen.getByText("35:37")).toBeInTheDocument();
  });

  it("is lit (no --dim class) by default", () => {
    render(<Nixie value={334} />);
    expect(screen.getByText("334")).toHaveClass("instrument-nixie");
    expect(screen.getByText("334")).not.toHaveClass("instrument-nixie--dim");
  });

  it("dims when the value isn't changing right now", () => {
    render(<Nixie value={57} dim />);
    expect(screen.getByText("57")).toHaveClass("instrument-nixie--dim");
  });

  it("keeps a clock steady when its value updates", () => {
    const { rerender } = render(<Nixie value="35:37" flickerOnChange={false} />);

    rerender(<Nixie value="35:38" flickerOnChange={false} />);

    expect(screen.getByText("35:38")).not.toHaveClass("instrument-nixie--flicker");
  });
});
