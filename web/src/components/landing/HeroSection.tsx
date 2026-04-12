import { Button } from "../ui";
import { AppScreenshotFrame } from "./AppScreenshotFrame";
import config from "../../lib/config";
import { useNavigate } from "react-router-dom";

interface HeroSectionProps {
  onScrollToHowItWorks: () => void;
  heroAnimationsEnabled: boolean;
  screenshotTheme: "warm" | "cool-pop";
}

export function HeroSection({
  onScrollToHowItWorks,
  heroAnimationsEnabled: _heroAnimationsEnabled,
  screenshotTheme,
}: HeroSectionProps) {
  const navigate = useNavigate();

  const handleStartFree = () => {
    document.getElementById("landing-install")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleHostedBeta = () => {
    window.location.href = "https://control.longhouse.ai";
  };

  return (
    <section className="landing-hero">
      <div className="landing-hero-split">
        <div className="landing-hero-text">
          <h1 className="landing-hero-headline">
            Mission control for your <span className="gradient-text">AI coding sessions.</span>
          </h1>

          <p className="landing-hero-subhead">
            One timeline for every Claude, Codex, and Gemini session.
            Find past work fast. Steer live sessions from anywhere.
          </p>

          <div className="landing-hero-ctas">
            <Button variant="primary" size="lg" className="landing-cta-main" onClick={handleStartFree}>
              Self-Host Free &rarr;
            </Button>
            <Button variant="secondary" size="lg" onClick={handleHostedBeta}>
              Hosted Beta
            </Button>
            {config.demoMode && (
              <Button variant="ghost" size="lg" onClick={() => navigate("/timeline")}>
                Try Live Demo
              </Button>
            )}
          </div>

          <p className="landing-hero-note">
            Open source. Runs on your laptop, VPS, or homelab.
            No account required to self-host.
          </p>

          <div className="landing-hero-cta-secondary">
            <Button variant="ghost" size="lg" className="landing-cta-text" onClick={onScrollToHowItWorks}>
              See how it works <span className="landing-cta-arrow">&darr;</span>
            </Button>
          </div>
        </div>

        <div className="landing-hero-visual">
          <AppScreenshotFrame
            src="/images/landing/timeline-preview.png"
            alt="Longhouse timeline showing Claude Code sessions"
            title="Session Timeline"
            aspectRatio="16/9"
            showChrome={true}
            theme={screenshotTheme}
            className="landing-hero-screenshot"
          />
        </div>
      </div>
    </section>
  );
}
