import { Button } from "../ui";
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
  screenshotTheme: _screenshotTheme,
}: HeroSectionProps) {
  const navigate = useNavigate();

  const handleStartFree = () => {
    document.getElementById("landing-install")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleHostedLater = () => {
    document.getElementById("pricing")?.scrollIntoView({ behavior: "smooth" });
  };

  return (
    <section className="landing-hero">
      <div className="landing-hero-split">
        <div className="landing-hero-text">
          <p className="landing-hero-kicker">Self-hosted session control</p>

          <h1 className="landing-hero-headline">
            Mission control for your <span className="gradient-text">AI coding sessions.</span>
          </h1>

          <p className="landing-hero-subhead">
            Bring Claude, Codex, and Gemini sessions into one timeline. Find past work fast,
            inspect the raw session, and steer live work later from browser, CLI, or API.
          </p>

          <div className="landing-hero-ctas">
            <Button variant="primary" size="lg" className="landing-cta-main" onClick={handleStartFree}>
              Self-Host Free &rarr;
            </Button>
            <Button variant="secondary" size="lg" onClick={handleHostedLater}>
              Hosted Later
            </Button>
            {config.demoMode && (
              <Button variant="ghost" size="lg" onClick={() => navigate("/timeline")}>
                Try Live Demo
              </Button>
            )}
          </div>

          <p className="landing-hero-note">
            Works on your laptop. Shines on a machine that stays on.
          </p>

          <div className="landing-hero-command-strip" aria-label="Longhouse launch example">
            <span className="landing-hero-command-label">Launch through Longhouse</span>
            <code className="landing-hero-command-code">longhouse claude</code>
          </div>

          <div className="landing-hero-cta-secondary">
            <Button variant="ghost" size="lg" className="landing-cta-text" onClick={onScrollToHowItWorks}>
              See the launch story <span className="landing-cta-arrow">&darr;</span>
            </Button>
          </div>
        </div>

        <div className="landing-hero-visual">
          <div className="landing-launch-panel">
            <div className="landing-launch-panel-header">
              <p className="landing-launch-panel-label">How it runs</p>
              <h2 className="landing-launch-panel-title">
                Work stays on your machine. Durability lives where you keep Longhouse online.
              </h2>
            </div>

            <div className="landing-launch-flow">
              <article className="landing-launch-node primary">
                <p className="landing-launch-node-eyebrow">Where work runs</p>
                <h3 className="landing-launch-node-title">Your dev machine</h3>
                <p className="landing-launch-node-copy">
                  Claude, Codex, or Gemini sessions run where you already work. The Machine Agent ships
                  the archive and keeps Longhouse in the launch path.
                </p>
              </article>

              <div className="landing-launch-bridge" aria-hidden="true">
                <span className="landing-launch-bridge-line" />
                <span className="landing-launch-bridge-label">ships sessions</span>
                <span className="landing-launch-bridge-line" />
              </div>

              <article className="landing-launch-node">
                <p className="landing-launch-node-eyebrow">Where durability lives</p>
                <h3 className="landing-launch-node-title">Runtime Host</h3>
                <p className="landing-launch-node-copy">
                  Run it on your laptop to try it. Move it to a VPS, Mac mini, or homelab box when you
                  want search and control later without depending on an awake laptop.
                </p>
              </article>
            </div>

            <div className="landing-launch-surface">
              <span className="landing-launch-surface-pill">Browser</span>
              <span className="landing-launch-surface-pill">CLI</span>
              <span className="landing-launch-surface-pill">/api/agents/*</span>
            </div>

            <div className="landing-launch-truths">
              <div className="landing-launch-truth">
                <span className="landing-launch-truth-title">Archive first</span>
                <p>Import existing sessions immediately and find prior work without changing workflow first.</p>
              </div>
              <div className="landing-launch-truth">
                <span className="landing-launch-truth-title">Control later</span>
                <p>Start through Longhouse when the session should stay reachable after launch.</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
