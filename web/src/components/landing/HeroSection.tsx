import { useNavigate } from "react-router-dom";
import { Button } from "../ui";
import { AppScreenshotFrame } from "./AppScreenshotFrame";
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
          <div className="landing-hero-badge">
            <span className="landing-hero-badge-dot" />
            One timeline for every session
          </div>

          <h1 className="landing-hero-headline">
            Control live sessions <span className="gradient-text">after launch.</span>
          </h1>

          <p className="landing-hero-subhead">
            Install Longhouse on the machine where work lives. Open one timeline for Claude Code, Codex,
            and Gemini sessions, find one prior session fast, and recover the context you need. When you
            want control later, start new work through Longhouse.
          </p>

          <p className="landing-hero-note">
            Works on your laptop. Shines on a machine that stays on. On macOS, Longhouse also lives in
            your menu bar. Self-host free where the work lives, or use hosted beta later if you want us
            to run the box.
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

          <div className="landing-hero-friction-reducers">
            <span>Find prior work first</span>
            <span className="landing-hero-friction-dot" aria-hidden="true" />
            <span>Start through Longhouse when you want control</span>
            <span className="landing-hero-friction-dot" aria-hidden="true" />
            <span>Browser and CLI stay in sync</span>
          </div>

          <div className="landing-hero-install" id="landing-install">
            <p className="landing-hero-install-label">Install Longhouse. Open it. Find one prior session.</p>
            <div className="landing-hero-install-grid">
              <pre className="landing-code-block">
                <code>{"curl -fsSL https://get.longhouse.ai/install.sh | bash\nlonghouse serve"}</code>
              </pre>
              <pre className="landing-code-block">
                <code>{"longhouse claude\nlonghouse codex"}</code>
              </pre>
            </div>
            <p className="landing-hero-install-note">
              One command installs the CLI and runs guided onboarding. Open the timeline and look for one
              prior session right away. On macOS, Longhouse also adds a menu bar app. Later, when you
              want a session to stay reachable after launch, start it with <code>longhouse claude</code>{" "}
              or <code>longhouse codex</code>. Use <code>longhouse serve --demo</code> only when you
              want a safe preview before importing real work.
            </p>
          </div>

          <div className="landing-hero-cta-secondary">
            <Button variant="ghost" size="lg" className="landing-cta-text" onClick={onScrollToHowItWorks}>
              See how it works <span className="landing-cta-arrow">↓</span>
            </Button>
          </div>
        </div>

        <div className="landing-hero-visual">
          <div className="landing-hero-signal-panel">
            <p className="landing-hero-signal-label">How people get value fast</p>
            <h2 className="landing-hero-signal-title">One timeline first. Then keep control.</h2>
            <div className="landing-hero-signal-list">
              <div className="landing-hero-signal-card">
                <p className="landing-hero-signal-card-title">Timeline</p>
                <p>Search prior work, inspect raw session detail, and recover the exact context that matters.</p>
              </div>
              <div className="landing-hero-signal-card">
                <p className="landing-hero-signal-card-title">Control channel</p>
                <p>Start through Longhouse when you want the session to stay reachable later from the browser or CLI.</p>
              </div>
              <div className="landing-hero-signal-card">
                <p className="landing-hero-signal-card-title">Coordination</p>
                <p>Timeline, browser actions, and the CLI all point at the same session.</p>
              </div>
            </div>
          </div>

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
