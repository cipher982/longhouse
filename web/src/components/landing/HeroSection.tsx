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
            Free local wedge first
          </div>

          <h1 className="landing-hero-headline">
            Find the session. <span className="gradient-text">Ask it. Continue it.</span>
          </h1>

          <p className="landing-hero-subhead">
            Longhouse turns Claude Code, Codex, and Gemini sessions into durable objects you can search,
            inspect, message, and resume.
          </p>

          <p className="landing-hero-note">
            Start free locally. Use hosted beta when you want always-on browser access. Claude remains the
            strongest direct continuation path today; Codex and Gemini are archive-first for now.
          </p>

          <div className="landing-hero-ctas">
            <Button variant="primary" size="lg" className="landing-cta-main" onClick={handleStartFree}>
              Start Free Locally &rarr;
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
            <span>No card for first value</span>
            <span className="landing-hero-friction-dot" aria-hidden="true" />
            <span>No keys needed for demo</span>
            <span className="landing-hero-friction-dot" aria-hidden="true" />
            <span>CLI / API first</span>
          </div>

          <div className="landing-hero-install" id="landing-install">
            <p className="landing-hero-install-label">Open the product in under 2 minutes</p>
            <div className="landing-hero-install-grid">
              <pre className="landing-code-block">
                <code>{"curl -fsSL https://get.longhouse.ai/install.sh | bash\nlonghouse serve --demo"}</code>
              </pre>
              <pre className="landing-code-block">
                <code>{"longhouse wall --json"}</code>
              </pre>
            </div>
            <p className="landing-hero-install-note">
              The bundled UI is the easiest way to look around, but the same kernel is scriptable from the
              terminal and from <code>/api/agents/*</code>.
            </p>
          </div>

          <div className="landing-hero-cta-secondary">
            <Button variant="ghost" size="lg" className="landing-cta-text" onClick={onScrollToHowItWorks}>
              See The Proof Journey <span className="landing-cta-arrow">↓</span>
            </Button>
          </div>
        </div>

        <div className="landing-hero-visual">
          <div className="landing-hero-signal-panel">
            <p className="landing-hero-signal-label">What the product actually is</p>
            <h2 className="landing-hero-signal-title">A session kernel with a bundled human view.</h2>
            <div className="landing-hero-signal-list">
              <div className="landing-hero-signal-card">
                <p className="landing-hero-signal-card-title">Continuity</p>
                <p>Search prior work, inspect the raw session detail, and recover the exact context that matters.</p>
              </div>
              <div className="landing-hero-signal-card">
                <p className="landing-hero-signal-card-title">Coordination</p>
                <p><code>wall</code>, <code>tail</code>, <code>peers</code>, <code>message</code>, and inbox state all live on the same seam.</p>
              </div>
              <div className="landing-hero-signal-card">
                <p className="landing-hero-signal-card-title">Continuation</p>
                <p>Continue the session from the machine surface or from the integrated web UI.</p>
              </div>
            </div>
          </div>

          <AppScreenshotFrame
            src="/images/landing/timeline-preview.png"
            alt="Longhouse session timeline showing Claude Code sessions"
            title="Bundled Human View"
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
