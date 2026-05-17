import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import SettingsPage from "../SettingsPage";

vi.mock("../../components/EmailConfigCard", () => ({
  default: () => <div>Email Config</div>,
}));

describe("SettingsPage", () => {
  it("renders only the active settings cards", () => {
    render(<SettingsPage />);

    expect(screen.getByText("Settings")).toBeTruthy();
    expect(screen.getByText("Email Config")).toBeTruthy();
    expect(screen.queryByText("Basic Information")).toBeNull();
    expect(screen.queryByText("Chat Tools")).toBeNull();
    expect(screen.queryByText("Custom Instructions")).toBeNull();
  });
});
