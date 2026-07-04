import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import SettingsPage from "../SettingsPage";

describe("SettingsPage", () => {
  it("redirects to the active device settings surface", () => {
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/settings/devices" element={<div>Device Settings</div>} />
        </Routes>
      </MemoryRouter>
    );

    expect(screen.getByText("Device Settings")).toBeTruthy();
  });
});
