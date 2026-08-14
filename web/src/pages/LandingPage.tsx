import { lazy, Suspense } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
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
// Lazy: pulls the recorded grids + terminal renderer, which must stay out of
// the main bundle (same discipline as HeroSection's lazy HeroDemo).
const SteerPlayground = lazy(() =>
  import("../components/landing/SteerPlayground").then(
    ({ SteerPlayground: Component }) => ({ default: Component }),
  ),
);
const RemoteWorkSceneSection = lazy(() =>
  import("../components/landing/RemoteWorkSceneSection").then(
    ({ RemoteWorkSceneSection: Component }) => ({ default: Component }),
  ),
);
import { MachineSurfaceSection } from "../components/landing/MachineSurfaceSection";
import { DemoSection } from "../components/landing/DemoSection";
import { IntegrationsSection } from "../components/landing/IntegrationsSection";
import { PricingSection } from "../components/landing/PricingSection";
import { TrustSection } from "../components/landing/TrustSection";
import { FooterCTA } from "../components/landing/FooterCTA";

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
      "Longhouse connects to coding-agent CLIs already on your machines. Watch any session live, search everything they have done, and control supported sessions from the web or your iPhone while the agent keeps running in its real terminal. Self-hosted and Apache-2.0.",
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
        <Suspense fallback={<section className="steer-playground" aria-hidden="true" />}>
          <SteerPlayground />
        </Suspense>
        <Suspense fallback={<section className="landing-remote-scene landing-remote-scene-fallback" aria-hidden="true" />}>
          <RemoteWorkSceneSection />
        </Suspense>
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
