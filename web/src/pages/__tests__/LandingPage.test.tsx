import { render, screen } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { config } from "../../lib/config";
import { TestRouter } from "../../test/test-utils";
import LandingPage from "../LandingPage";

const authMocks = vi.hoisted(() => ({
  useAuth: vi.fn(),
}));

vi.mock("../../lib/auth", () => ({
  useAuth: authMocks.useAuth,
}));

vi.mock("../../hooks/usePublicPageScroll", () => ({
  usePublicPageScroll: vi.fn(),
}));

vi.mock("../../components/landing/LandingHeader", () => ({
  LandingHeader: ({ onSignIn, onGetStarted }: { onSignIn: () => void; onGetStarted: () => void }) => (
    <div>
      <button type="button" onClick={onSignIn}>
        Sign In
      </button>
      <button type="button" onClick={onGetStarted}>
        Get Started
      </button>
    </div>
  ),
}));

vi.mock("../../components/landing/HeroSection", () => ({
  HeroSection: () => <div>Hero Section</div>,
}));

vi.mock("../../components/landing/RemoteWorkSceneSection", () => ({
  RemoteWorkSceneSection: () => <div>Remote Work Scene</div>,
}));

vi.mock("../../components/landing/MachineSurfaceSection", () => ({
  MachineSurfaceSection: () => <div>Machine Surface</div>,
}));

vi.mock("../../components/landing/DemoSection", () => ({
  DemoSection: () => <div>Demo Section</div>,
}));

vi.mock("../../components/landing/IntegrationsSection", () => ({
  IntegrationsSection: () => <div>Integrations Section</div>,
}));

vi.mock("../../components/landing/PricingSection", () => ({
  PricingSection: () => <div>Pricing Section</div>,
}));

vi.mock("../../components/landing/TrustSection", () => ({
  TrustSection: () => <div>Trust Section</div>,
}));

vi.mock("../../components/landing/FooterCTA", () => ({
  FooterCTA: () => <div>Footer CTA</div>,
}));

function renderLandingPage(initialEntry = "/") {
  return render(
    <TestRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/timeline" element={<div>Timeline Home</div>} />
      </Routes>
    </TestRouter>,
  );
}

describe("LandingPage", () => {
  const originalAuthEnabled = config.authEnabled;
  const originalDemoMode = config.demoMode;

  beforeEach(() => {
    document.body.innerHTML = "";
    const root = document.createElement("div");
    root.id = "react-root";
    document.body.appendChild(root);

    config.authEnabled = true;
    config.demoMode = false;
    authMocks.useAuth.mockReturnValue({
      isAuthenticated: false,
      isLoading: false,
    });
  });

  afterEach(() => {
    config.authEnabled = originalAuthEnabled;
    config.demoMode = originalDemoMode;
    vi.restoreAllMocks();
  });

  it("redirects authenticated users to the timeline", async () => {
    authMocks.useAuth.mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
    });

    renderLandingPage("/");

    expect(await screen.findByText("Timeline Home")).toBeInTheDocument();
  });

});
