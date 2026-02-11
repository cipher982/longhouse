import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../ui";
import { AppScreenshotFrame } from "./AppScreenshotFrame";
import { InstallSection } from "./InstallSection";
import { WaitlistModal } from "./WaitlistModal";
import config from "../../lib/config";

interface HeroSectionProps {
  onScrollToHowItWorks: () => void;
  heroAnimationsEnabled: boolean;
}

export function HeroSection({ onScrollToHowItWorks, heroAnimationsEnabled: _heroAnimationsEnabled }: HeroSectionProps) {
  const navigate = useNavigate();
  const [showWaitlist, setShowWaitlist] = useState(false);

  const handleSelfHost = () => {
    document.querySelector(".install-section")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleHostedBeta = () => {
    setShowWaitlist(true);
  };

  return (
    <section className="landing-hero">
      <div className="landing-hero-split">
        {/* Left: Text content */}
        <div className="landing-hero-text">
          <div className="landing-hero-badge">
            <span className="landing-hero-badge-dot" />
            Free during beta
          </div>

          <h1 className="landing-hero-headline">
            Never lose an <span className="gradient-text">AI coding conversation.</span>
          </h1>

          <p className="landing-hero-subhead">
            Claude Code, Codex, and Gemini sessions in one searchable timeline.
          </p>

          <p className="landing-hero-note">
            Local-first. Self-host anytime. Hosted beta waitlist open.
          </p>

          <div className="landing-hero-ctas">
            <Button variant="primary" size="lg" className="landing-cta-main" onClick={handleSelfHost}>
              Self-host Now
            </Button>
            {config.demoMode ? (
              <Button variant="secondary" size="lg" onClick={() => navigate("/timeline")}>
                Try Live Demo &rarr;
              </Button>
            ) : (
              <Button variant="secondary" size="lg" onClick={handleHostedBeta}>
                Hosted Beta &rarr;
              </Button>
            )}
          </div>

          <div className="landing-hero-friction-reducers">
            <span>Works offline</span>
            <span className="landing-hero-friction-dot" aria-hidden="true" />
            <span>&lt;2min setup</span>
            <span className="landing-hero-friction-dot" aria-hidden="true" />
            <span>Your data stays local</span>
          </div>

          {/* Install command section - self-host path */}
          <InstallSection className="landing-hero-install" />

          <div className="landing-hero-cta-secondary">
            <Button variant="ghost" size="lg" className="landing-cta-text" onClick={onScrollToHowItWorks}>
              See How It Works <span className="landing-cta-arrow">↓</span>
            </Button>
          </div>
        </div>

        {/* Right: Product screenshot */}
        <div className="landing-hero-visual">
          <AppScreenshotFrame
            src="/images/landing/dashboard-preview.png"
            alt="Longhouse session timeline showing Claude Code sessions"
            title="Longhouse"
            aspectRatio="4/3"
            showChrome={true}
            className="landing-hero-screenshot"
          />
        </div>
      </div>

      {showWaitlist && <WaitlistModal onClose={() => setShowWaitlist(false)} source="hero_hosted" />}
    </section>
  );
}
