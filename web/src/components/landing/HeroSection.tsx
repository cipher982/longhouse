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
            Existing sessions first
          </div>

          <h1 className="landing-hero-headline">
            Control live sessions <span className="gradient-text">after launch.</span>
          </h1>

          <p className="landing-hero-subhead">
            Import existing Claude Code, Codex, and Gemini sessions into one searchable timeline, then
            start Longhouse sessions you can inspect, message, and continue later.
          </p>

          <p className="landing-hero-note">
            Works on your laptop. Shines on a machine that stays on. Self-host free where the work lives,
            or use hosted later. Claude remains the strongest direct continuation path today.
          </p>

          <div className="landing-hero-ctas">
            <Button variant="primary" size="lg" className="landing-cta-main" onClick={handleStartFree}>
              Self-Host Free &rarr;
            </Button>
            <Button variant="secondary" size="lg" onClick={handleHostedBeta}>
              Hosted Later
            </Button>
            {config.demoMode && (
              <Button variant="ghost" size="lg" onClick={() => navigate("/timeline")}>
                Try Live Demo
              </Button>
            )}
          </div>

          <div className="landing-hero-friction-reducers">
            <span>Import existing sessions first</span>
            <span className="landing-hero-friction-dot" aria-hidden="true" />
            <span>Control new Longhouse sessions second</span>
            <span className="landing-hero-friction-dot" aria-hidden="true" />
            <span>CLI / API first</span>
          </div>

          <div className="landing-hero-install" id="landing-install">
            <p className="landing-hero-install-label">Get from install to first useful session in minutes</p>
            <div className="landing-hero-install-grid">
              <pre className="landing-code-block">
                <code>{"curl -fsSL https://get.longhouse.ai/install.sh | bash\nlonghouse serve --demo"}</code>
              </pre>
              <pre className="landing-code-block">
                <code>{"longhouse connect --install\nlonghouse wall --json"}</code>
              </pre>
            </div>
            <p className="landing-hero-install-note">
              Existing sessions become findable immediately. New Longhouse sessions become controllable
              after launch from the same browser, CLI, and <code>/api/agents/*</code> surface.
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
            <p className="landing-hero-signal-label">Two-beat product loop</p>
            <h2 className="landing-hero-signal-title">Find old sessions first. Control new ones second.</h2>
            <div className="landing-hero-signal-list">
              <div className="landing-hero-signal-card">
                <p className="landing-hero-signal-card-title">Findability</p>
                <p>Search prior work, inspect raw session detail, and recover the exact context that matters.</p>
              </div>
              <div className="landing-hero-signal-card">
                <p className="landing-hero-signal-card-title">Control</p>
                <p>Start a Longhouse session, then message it or continue it later from browser, CLI, or API.</p>
              </div>
              <div className="landing-hero-signal-card">
                <p className="landing-hero-signal-card-title">Coordination</p>
                <p><code>wall</code>, <code>tail</code>, <code>peers</code>, <code>message</code>, and inbox state all live on the same seam.</p>
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
