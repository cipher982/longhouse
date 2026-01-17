import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import config from "../lib/config";
import { SwarmLogo } from "../components/SwarmLogo";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import "../styles/landing.css";

// Section components
import { HeroSection } from "../components/landing/HeroSection";
import { HowItWorksSection } from "../components/landing/HowItWorksSection";
import { DemoSection } from "../components/landing/DemoSection";
import { IntegrationsSection } from "../components/landing/IntegrationsSection";
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
    <div
      style={{
        position: "fixed",
        right: 12,
        bottom: 12,
        zIndex: 9999,
        padding: "10px 12px",
        borderRadius: 10,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
        fontSize: 12,
        lineHeight: 1.3,
        color: "rgba(255,255,255,0.9)",
        background: "rgba(0,0,0,0.8)",
        border: "1px solid rgba(255,255,255,0.12)",
        pointerEvents: "none",
      }}
    >
      <div>{`fps ~ ${stats.fps.toFixed(0)}`}</div>
      <div>{`avg ${stats.avgMs.toFixed(1)}ms`}</div>
      <div>{`p95 ${stats.p95Ms.toFixed(1)}ms`}</div>
      <div style={{ marginTop: 6, opacity: 0.8 }}>{`fx: ${fxText}`}</div>
    </div>
  );
}

export default function LandingPage() {
  const { isAuthenticated, isLoading } = useAuth();
  const navigate = useNavigate();

  const { fxEnabled, showPerfHud } = useMemo(() => getLandingFxFromUrl(), []);
  const particlesEnabled = fxEnabled.has("particles");
  const heroAnimationsEnabled = fxEnabled.has("hero");

  // Enable normal document scrolling (app shell locks root by default)
  usePublicPageScroll();

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

  // If already logged in, redirect to dashboard
  // SKIP redirect when:
  // - authEnabled=false (dev mode - fake auth, don't redirect)
  // - noredirect=1 query param (manual override for testing)
  useEffect(() => {
    if (!config.authEnabled) return; // Dev mode: never redirect
    const params = new URLSearchParams(window.location.search);
    const skipRedirect = params.get("noredirect") === "1";
    if (isAuthenticated && !isLoading && !skipRedirect) {
      navigate("/dashboard");
    }
  }, [isAuthenticated, isLoading, navigate]);

  // Show loading while checking auth
  if (isLoading) {
    return (
      <div className="landing-loading">
        <SwarmLogo size={64} className="landing-loading-logo" />
      </div>
    );
  }

  const scrollToHowItWorks = () => {
    document.getElementById("how-it-works")?.scrollIntoView({ behavior: "smooth" });
  };

  return (
    <div className="landing-page" data-fx-hero={heroAnimationsEnabled ? "1" : "0"} data-fx-particles={particlesEnabled ? "1" : "0"}>
      {/* Particle background */}
      {particlesEnabled && <div className="particle-bg" />}

      {/* Gradient orb behind hero */}
      <div className="landing-glow-orb" />

      <main className="landing-main">
        <HeroSection onScrollToHowItWorks={scrollToHowItWorks} heroAnimationsEnabled={heroAnimationsEnabled} />
        <HowItWorksSection />
        <DemoSection />
        <IntegrationsSection />
        <PricingSection />
        <TrustSection />
        <FooterCTA />
      </main>

      {showPerfHud && <LandingPerfHud fxEnabled={fxEnabled} />}
    </div>
  );
}
