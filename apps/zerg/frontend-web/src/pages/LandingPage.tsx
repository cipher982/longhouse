import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import config from "../lib/config";
import { SwarmLogo } from "../components/SwarmLogo";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import "../styles/landing.css";

// Section components
import { LandingHeader } from "../components/landing/LandingHeader";
import { HeroSection } from "../components/landing/HeroSection";
import { HowItWorksSection } from "../components/landing/HowItWorksSection";
import { DemoSection } from "../components/landing/DemoSection";
import { IntegrationsSection } from "../components/landing/IntegrationsSection";
import { SkillsSection } from "../components/landing/SkillsSection";
import { PricingSection } from "../components/landing/PricingSection";
import { TrustSection } from "../components/landing/TrustSection";
import { FooterCTA } from "../components/landing/FooterCTA";

type LandingFxName = "particles" | "hero";

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

function getLandingFxFromUrl(): { fxEnabled: Set<LandingFxName>; showPerfHud: boolean } {
  if (typeof window === "undefined") {
    return { fxEnabled: new Set<LandingFxName>(["particles", "hero"]), showPerfHud: false };
  }

  const params = new URLSearchParams(window.location.search);
  const fxEnabled = parseFxParam(params.get("fx")) ?? new Set<LandingFxName>(["particles", "hero"]);
  const perfRaw = (params.get("perf") ?? "").trim().toLowerCase();
  const showPerfHud = perfRaw === "1" || perfRaw === "true" || perfRaw === "yes";
  return { fxEnabled, showPerfHud };
}

function LandingPerfHud({
  fxEnabled,
}: {
  fxEnabled: Set<LandingFxName>;
}) {
  const [stats, setStats] = useState<{ fps: number; avgMs: number; p95Ms: number } | null>(null);

  useEffect(() => {
    const frameTimes: number[] = [];
    let last = performance.now();
    let rafId = 0;
    let intervalId = 0;

    const onFrame = (now: number) => {
      const dt = now - last;
      last = now;
      frameTimes.push(dt);
      if (frameTimes.length > 240) frameTimes.shift();
      rafId = requestAnimationFrame(onFrame);
    };

    rafId = requestAnimationFrame(onFrame);

    intervalId = window.setInterval(() => {
      if (frameTimes.length < 5) return;
      const times = [...frameTimes].sort((a, b) => a - b);
      const avgMs = times.reduce((sum, t) => sum + t, 0) / times.length;
      const p95Ms = times[Math.floor(times.length * 0.95)] ?? avgMs;
      const fps = avgMs > 0 ? 1000 / avgMs : 0;
      setStats({ fps, avgMs, p95Ms });
    }, 500);

    return () => {
      cancelAnimationFrame(rafId);
      window.clearInterval(intervalId);
    };
  }, []);

  if (!stats) return null;

  const fxText = Array.from(fxEnabled).sort().join(", ") || "none";

  return (
    <div className="landing-perf-hud">
      <div>{`fps ~ ${stats.fps.toFixed(0)}`}</div>
      <div>{`avg ${stats.avgMs.toFixed(1)}ms`}</div>
      <div>{`p95 ${stats.p95Ms.toFixed(1)}ms`}</div>
      <div className="landing-perf-hud-fx">{`fx: ${fxText}`}</div>
    </div>
  );
}

export default function LandingPage() {
  const { isAuthenticated, isLoading, refreshAuth } = useAuth();
  const navigate = useNavigate();
  const [isAcceptingToken, setIsAcceptingToken] = useState(false);

  const { fxEnabled, showPerfHud } = useMemo(() => getLandingFxFromUrl(), []);
  const particlesEnabled = fxEnabled.has("particles");
  const heroAnimationsEnabled = fxEnabled.has("hero");

  // Enable normal document scrolling (app shell locks root by default)
  usePublicPageScroll();

  // Handle auth token from URL parameter (for cross-domain auth redirects)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const authToken = params.get('auth_token');

    if (!authToken) return;

    setIsAcceptingToken(true);

    // Call backend to validate token and set cookie
    fetch(`${config.apiBaseUrl}/auth/accept-token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ token: authToken }),
    })
      .then(async (response) => {
        if (response.ok) {
          // Token accepted, cookie set - redirect to timeline
          if (refreshAuth) await refreshAuth();
          window.location.href = '/timeline';
        } else {
          console.error('Token acceptance failed:', await response.text());
          params.delete('auth_token');
          window.history.replaceState({}, '', window.location.pathname);
          setIsAcceptingToken(false);
        }
      })
      .catch((error) => {
        console.error('Token acceptance error:', error);
        setIsAcceptingToken(false);
      });
  }, [refreshAuth]);

  // Control global UI effects based on landing page effects
  useEffect(() => {
    const hasAnyEffect = particlesEnabled || heroAnimationsEnabled;
    const container = document.getElementById("react-root");
    const previous = container?.getAttribute("data-ui-effects");
    if (container) {
      container.setAttribute("data-ui-effects", hasAnyEffect ? "on" : "off");
    }
    return () => {
      if (!container) return;
      if (previous) container.setAttribute("data-ui-effects", previous);
      else container.removeAttribute("data-ui-effects");
    };
  }, [particlesEnabled, heroAnimationsEnabled]);

  // If already logged in, redirect to timeline
  // SKIP redirect when:
  // - authEnabled=false (dev mode - fake auth, don't redirect)
  // - noredirect=1 query param (manual override for testing)
  useEffect(() => {
    if (!config.authEnabled || config.marketingOnly) return; // Dev/marketing: never redirect
    const params = new URLSearchParams(window.location.search);
    const skipRedirect = params.get("noredirect") === "1";
    if (isAuthenticated && !isLoading && !skipRedirect) {
      navigate("/timeline");
    }
  }, [isAuthenticated, isLoading, navigate]);

  // Show loading while checking auth or accepting token
  if (isLoading || isAcceptingToken) {
    return (
      <div className="landing-loading">
        <SwarmLogo size={64} className="landing-loading-logo" />
        {isAcceptingToken && <p style={{ marginTop: '1rem', color: 'var(--color-text-secondary)' }}>Signing in...</p>}
      </div>
    );
  }

  const scrollToHowItWorks = () => {
    document.getElementById("how-it-works")?.scrollIntoView({ behavior: "smooth" });
  };

  const scrollToPricing = () => {
    document.getElementById("pricing")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleSignIn = () => {
    // Trigger the sign-in modal via the Get Started button in hero
    const signInBtn = document.querySelector('.landing-cta-main') as HTMLButtonElement;
    signInBtn?.click();
  };

  return (
    <div className="landing-page" data-fx-hero={heroAnimationsEnabled ? "1" : "0"} data-fx-particles={particlesEnabled ? "1" : "0"}>
      {/* Sticky Header */}
      <LandingHeader onSignIn={handleSignIn} onGetStarted={scrollToPricing} />

      {/* Particle background */}
      {particlesEnabled && <div className="particle-bg" />}

      {/* Gradient orb behind hero */}
      <div className="landing-glow-orb" />

      <main className="landing-main">
        <HeroSection onScrollToHowItWorks={scrollToHowItWorks} heroAnimationsEnabled={heroAnimationsEnabled} />
        <HowItWorksSection />
        <DemoSection />
        <IntegrationsSection />
        <SkillsSection />
        <PricingSection />
        <TrustSection />
        <FooterCTA />
      </main>

      {showPerfHud && <LandingPerfHud fxEnabled={fxEnabled} />}
    </div>
  );
}
