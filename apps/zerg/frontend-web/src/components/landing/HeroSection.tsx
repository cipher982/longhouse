import { useNavigate } from "react-router-dom";
import { Button } from "../ui";
import { AppScreenshotFrame } from "./AppScreenshotFrame";
import { InstallSection } from "./InstallSection";
import config from "../../lib/config";

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

  const handleSelfHost = () => {
    document.querySelector(".install-section")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleHostedBeta = () => {
    window.location.href = "https://control.longhouse.ai";
  };

  return (
    <section className="landing-hero">
      <div className="landing-hero-split">
        {/* Left: Text content */}
        <div className="landing-hero-text">
          <div className="landing-hero-badge">
            <span className="landing-hero-badge-dot" />
            Self-host free forever
          </div>

          <h1 className="landing-hero-headline">
            Your coding agents, <span className="gradient-text">always on, from anywhere.</span>
          </h1>

          <p className="landing-hero-subhead">
            Close your laptop. Your agents keep running. Resume from any device.
          </p>

          <p className="landing-hero-note">
            Claude Code, Codex, and Gemini &mdash; one timeline, fully interactive. Self-host free, or we host it for $5/mo.
          </p>

          <div className="landing-hero-ctas">
            <Button variant="primary" size="lg" className="landing-cta-main" onClick={handleHostedBeta}>
              Get Started &rarr;
            </Button>
            {config.demoMode ? (
              <Button variant="secondary" size="lg" onClick={() => navigate("/timeline")}>
                Try Live Demo &rarr;
              </Button>
            ) : (
              <Button variant="secondary" size="lg" onClick={handleSelfHost}>
                Self-host Free
              </Button>
            )}
          </div>

          <div className="landing-hero-friction-reducers">
            <span>Always-on agents</span>
            <span className="landing-hero-friction-dot" aria-hidden="true" />
            <span>&lt;2min setup</span>
            <span className="landing-hero-friction-dot" aria-hidden="true" />
            <span>Bring your own API keys</span>
          </div>

          {/* Install command section - self-host path */}
          <InstallSection className="landing-hero-install" />

          <div className="landing-hero-cta-secondary">
            <Button variant="ghost" size="lg" className="landing-cta-text" onClick={onScrollToHowItWorks}>
              See How It Works <span className="landing-cta-arrow">↓</span>
            </Button>
          </div>

          <a
            href="mailto:hello@longhouse.ai?subject=Enterprise%20inquiry"
            className="landing-hero-enterprise-link"
          >
            Enterprise? Contact us &rarr;
          </a>
        </div>

        {/* Right: Product screenshot */}
        <div className="landing-hero-visual">
          <AppScreenshotFrame
            src="/images/landing/timeline-preview.png"
            alt="Longhouse session timeline showing Claude Code sessions"
            title="Longhouse"
            aspectRatio="4/3"
            showChrome={true}
            theme={screenshotTheme}
            className="landing-hero-screenshot"
          />
        </div>
      </div>

    </section>
  );
}
