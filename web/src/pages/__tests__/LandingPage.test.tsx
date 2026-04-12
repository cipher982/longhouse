import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes, useLocation } from "react-router-dom";
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

vi.mock("../../components/landing/KernelThesisSection", () => ({
  KernelThesisSection: () => <div>Kernel Thesis</div>,
}));

vi.mock("../../components/landing/HowItWorksSection", () => ({
  HowItWorksSection: () => <div>How It Works</div>,
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

vi.mock("../../components/landing/InstallSection", () => ({
  InstallSection: () => <div>Install Section</div>,
}));

vi.mock("../../components/landing/FooterCTA", () => ({
  FooterCTA: () => <div>Footer CTA</div>,
}));

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
}

function renderLandingPage(initialEntry = "/") {
  return render(
    <TestRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/timeline" element={<div>Timeline Home</div>} />
      </Routes>
      <LocationProbe />
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

  it("redirects authenticated users to the timeline unless noredirect is set", async () => {
    authMocks.useAuth.mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
    });

    renderLandingPage("/");

    expect(await screen.findByText("Timeline Home")).toBeInTheDocument();
  });

  it("keeps authenticated users on landing when noredirect=1", async () => {
    authMocks.useAuth.mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
    });

    renderLandingPage("/?noredirect=1");

    expect(await screen.findByText("Hero Section")).toBeInTheDocument();
    expect(screen.queryByText("Timeline Home")).not.toBeInTheDocument();
  });

  it("treats screenshot theme as URL-owned state", async () => {
    const user = userEvent.setup();

    renderLandingPage("/?marketing=true&screenshot_theme=warm");

    expect(await screen.findByText("Hero Section")).toBeInTheDocument();
    expect(document.querySelector(".landing-page")).toHaveAttribute("data-screenshot-theme", "warm");

    await user.click(screen.getByRole("button", { name: "Cool Pop" }));

    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent(
        /\?marketing=true&screenshot_theme=cool-pop|\\?screenshot_theme=cool-pop&marketing=true/,
      );
    });
    expect(document.querySelector(".landing-page")).toHaveAttribute("data-screenshot-theme", "cool-pop");
  });

  it("updates the root ui-effects attribute from URL-driven fx state", async () => {
    renderLandingPage("/?fx=none");

    expect(await screen.findByText("Hero Section")).toBeInTheDocument();
    expect(document.getElementById("react-root")).toHaveAttribute("data-ui-effects", "off");
  });
});
