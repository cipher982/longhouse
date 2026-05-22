import { useMemo } from "react";
import { Navigate, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../lib/auth";
import config from "../lib/config";
import { SwarmLogo } from "../components/SwarmLogo";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import { useRootUiEffects } from "../hooks/useRootUiEffects";
import { usePageMeta } from "../hooks/usePageMeta";
import "../styles/landing.css";

// Section components
import { LandingHeader } from "../components/landing/LandingHeader";
import { HeroSection } from "../components/landing/HeroSection";
import { KernelThesisSection } from "../components/landing/KernelThesisSection";
import { MachineSurfaceSection } from "../components/landing/MachineSurfaceSection";
import { DemoSection } from "../components/landing/DemoSection";
import { IntegrationsSection } from "../components/landing/IntegrationsSection";
import { PricingSection } from "../components/landing/PricingSection";
import { TrustSection } from "../components/landing/TrustSection";
import { FooterCTA } from "../components/landing/FooterCTA";
import { LandingPerfHud } from "../components/landing/LandingPerfHud";

type LandingFxName = "particles" | "hero";
type ScreenshotFrameTheme = "warm" | "cool-pop";

function parseFxParam(value: string | null): Set<LandingFxName> | null {
  if (!value) return null;
  const normalized = value.trim().toLowerCase();
  if (!normalized || normalized === "all") return new Set<LandingFxName>(["particles", "hero"]);
  if (normalized === "none" || normalized === "off") return new Set<LandingFxName>();

  const parts = normalized
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);

  const enabled = new Set<LandingFxName>();
  for (const part of parts) {
    if (part === "particles") enabled.add("particles");
    if (part === "hero" || part === "hero-anim" || part === "heroanim") enabled.add("hero");
  }
  return enabled;
}

function parseScreenshotThemeParam(value: string | null): ScreenshotFrameTheme {
  const normalized = (value ?? "").trim().toLowerCase();
  if (normalized === "cool" || normalized === "cool-pop" || normalized === "pop" || normalized === "vivid") {
    return "cool-pop";
  }
  return "warm";
}

function getLandingStateFromSearch(search: string): {
  fxEnabled: Set<LandingFxName>;
  showPerfHud: boolean;
  screenshotTheme: ScreenshotFrameTheme;
  showScreenshotThemeToggle: boolean;
  disableRedirect: boolean;
} {
  const params = new URLSearchParams(search);
  const fxEnabled = parseFxParam(params.get("fx")) ?? new Set<LandingFxName>(["particles", "hero"]);
  const perfRaw = (params.get("perf") ?? "").trim().toLowerCase();
  const showPerfHud = perfRaw === "1" || perfRaw === "true" || perfRaw === "yes";
  const screenshotTheme = parseScreenshotThemeParam(params.get("screenshot_theme"));
  const showScreenshotThemeToggle = params.get("marketing") === "true";
  const disableRedirect = params.get("noredirect") === "1";
  return { fxEnabled, showPerfHud, screenshotTheme, showScreenshotThemeToggle, disableRedirect };
}

export default function LandingPage() {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [, setSearchParams] = useSearchParams();

  const {
    fxEnabled,
    showPerfHud,
    screenshotTheme,
    showScreenshotThemeToggle,
    disableRedirect,
  } = useMemo(() => getLandingStateFromSearch(location.search), [location.search]);
  const particlesEnabled = fxEnabled.has("particles");
  const heroAnimationsEnabled = fxEnabled.has("hero");
  const fxText = useMemo(() => Array.from(fxEnabled).sort().join(", ") || "none", [fxEnabled]);

  // Enable normal document scrolling (app shell locks root by default)
  usePublicPageScroll();
  useRootUiEffects(particlesEnabled || heroAnimationsEnabled);
  usePageMeta({
    title: "Longhouse - Mission control for your AI coding sessions",
    description:
      "Bring Claude Code, Codex CLI, and Antigravity CLI sessions into one timeline, find past work fast, and steer live sessions later.",
  });

  // Show loading while checking auth or accepting token
  if (isLoading) {
    return (
      <div className="landing-loading">
        <SwarmLogo size={64} className="landing-loading-logo" />
      </div>
    );
  }

  if (config.authEnabled && !config.demoMode && !disableRedirect && isAuthenticated) {
    return <Navigate to="/timeline" replace />;
  }

  const scrollToInstall = () => {
    document.getElementById("landing-install")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleSignIn = () => {
    if (config.demoMode) {
      // Demo site: go to the hosted control plane auth
      window.location.href = "https://control.longhouse.ai";
    } else {
      navigate("/login");
    }
  };

  const handleScreenshotThemeChange = (nextTheme: ScreenshotFrameTheme) => {
    const params = new URLSearchParams(location.search);
    params.set("screenshot_theme", nextTheme);
    setSearchParams(params, { replace: true });
  };

  return (
    <div
      className="landing-page"
      data-fx-hero={heroAnimationsEnabled ? "1" : "0"}
      data-fx-particles={particlesEnabled ? "1" : "0"}
      data-screenshot-theme={screenshotTheme}
    >
      {/* Sticky Header */}
      <LandingHeader onSignIn={handleSignIn} onGetStarted={scrollToInstall} />

      {/* Particle background */}
      {particlesEnabled && <div className="particle-bg" />}

      {/* Gradient orb behind hero */}
      <div className="landing-glow-orb" />

      <main className="landing-main">
        <HeroSection />
        <DemoSection screenshotTheme={screenshotTheme} />
        <KernelThesisSection />
        <MachineSurfaceSection />
        <IntegrationsSection />
        <PricingSection />
        <TrustSection />
        <FooterCTA />
      </main>

      {showScreenshotThemeToggle && (
        <div className="landing-screenshot-theme-toggle" role="group" aria-label="Screenshot frame theme">
          <span className="landing-screenshot-theme-label">Screenshot frame</span>
          <button
            type="button"
            className={`landing-screenshot-theme-button warm ${screenshotTheme === "warm" ? "active" : ""}`}
            onClick={() => handleScreenshotThemeChange("warm")}
          >
            Warm
          </button>
          <button
            type="button"
            className={`landing-screenshot-theme-button cool-pop ${screenshotTheme === "cool-pop" ? "active" : ""}`}
            onClick={() => handleScreenshotThemeChange("cool-pop")}
          >
            Cool Pop
          </button>
        </div>
      )}

      {showPerfHud && <LandingPerfHud fxText={fxText} />}
    </div>
  );
}
