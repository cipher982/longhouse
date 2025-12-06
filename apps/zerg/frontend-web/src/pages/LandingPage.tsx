import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth, GoogleSignInButton, LoginOverlay } from "../lib/auth";
import { SwarmLogo } from "../components/SwarmLogo";
import config from "../lib/config";
import "../styles/landing.css";

// Section components
import { HeroSection } from "../components/landing/HeroSection";
import { PASSection } from "../components/landing/PASSection";
import { ScenariosSection } from "../components/landing/ScenariosSection";
import { DifferentiationSection } from "../components/landing/DifferentiationSection";
import { NerdSection } from "../components/landing/NerdSection";
import { IntegrationsSection } from "../components/landing/IntegrationsSection";
import { TrustSection } from "../components/landing/TrustSection";
import { FooterCTA } from "../components/landing/FooterCTA";

export default function LandingPage() {
  const { isAuthenticated, isLoading } = useAuth();
  const navigate = useNavigate();

  // If already logged in, redirect to dashboard
  useEffect(() => {
    if (isAuthenticated && !isLoading) {
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

  const scrollToScenarios = () => {
    document.getElementById("scenarios")?.scrollIntoView({ behavior: "smooth" });
  };

  return (
    <div className="landing-page">
      {/* Particle background */}
      <div className="particle-bg" />

      {/* Gradient orb behind hero */}
      <div className="landing-glow-orb" />

      <main className="landing-main">
        <HeroSection onScrollToScenarios={scrollToScenarios} />
        <PASSection />
        <ScenariosSection />
        <DifferentiationSection />
        <NerdSection />
        <IntegrationsSection />
        <TrustSection />
        <FooterCTA />
      </main>
    </div>
  );
}
