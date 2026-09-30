import { lazy } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";
import { useAuth } from "@/features/auth/auth";
import config from "@/shared/lib/config";
import { SwarmLogo } from "@/shared/ui/SwarmLogo";
import { AfterHydration } from "../AfterHydration";
import { usePublicPageScroll } from "../usePublicPageScroll";
import { useRootUiEffects } from "../useRootUiEffects";
import { usePageMeta } from "@/shared/hooks/usePageMeta";
import "./landing.css";

// Section components
import { LandingHeader } from "./LandingHeader";
import { HeroSection } from "./HeroSection";
// Lazy: pulls the recorded grids + terminal renderer, which must stay out of
// the main bundle (same discipline as HeroSection's lazy HeroDemo).
const SteerPlayground = lazy(() =>
  import("./SteerPlayground").then(
    ({ SteerPlayground: Component }) => ({ default: Component }),
  ),
);
const RemoteWorkSceneSection = lazy(() =>
  import("./RemoteWorkSceneSection").then(
    ({ RemoteWorkSceneSection: Component }) => ({ default: Component }),
  ),
);
import { MachineSurfaceSection } from "./MachineSurfaceSection";
import { DemoSection } from "./DemoSection";
import { IntegrationsSection } from "./IntegrationsSection";
import { PricingSection } from "./PricingSection";
import { TrustSection } from "./TrustSection";
import { FooterCTA } from "./FooterCTA";

export default function LandingPage() {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();

  // Enable normal document scrolling (app shell locks root by default)
  usePublicPageScroll();
  useRootUiEffects(true);
  usePageMeta({
    title: "Longhouse - Remote control for your coding agents",
    description:
      "Longhouse connects to coding-agent CLIs already on your machines. Watch sessions live, search past work, and control supported sessions from the web while the agent runs on your machine. Self-hosted and Apache-2.0.",
  });

  // Auth only matters when it can redirect us to /timeline. When no redirect
  // is possible (preview route or demo mode), render the
  // marketing page immediately — a slow or unreachable API must never leave
  // visitors on a spinner.
  const isPreviewRoute = location.pathname === "/landing";
  const redirectPossible =
    config.authEnabled && !config.demoMode && !isPreviewRoute;

  if (isLoading && redirectPossible) {
    return (
      <div className="landing-loading">
        <SwarmLogo size={64} className="landing-loading-logo" />
      </div>
    );
  }

  if (redirectPossible && isAuthenticated) {
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

  return (
    <div className="landing-page">
      {/* Sticky Header */}
      <LandingHeader onSignIn={handleSignIn} onGetStarted={scrollToInstall} />

      {/* Particle background */}
      <div className="particle-bg" />

      {/* Gradient orb behind hero */}
      <div className="landing-glow-orb" />

      <main className="landing-main">
        <HeroSection />
        <AfterHydration fallback={<section className="steer-playground" aria-hidden="true" />}>
          <SteerPlayground />
        </AfterHydration>
        <AfterHydration fallback={<section className="landing-remote-scene landing-remote-scene-fallback" aria-hidden="true" />}>
          <RemoteWorkSceneSection />
        </AfterHydration>
        <DemoSection />
        <IntegrationsSection />
        <MachineSurfaceSection />
        <PricingSection />
        <TrustSection />
        <FooterCTA />
      </main>

    </div>
  );
}
