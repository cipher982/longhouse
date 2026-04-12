import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { Button } from "../components/ui";
import { usePageMeta } from "../hooks/usePageMeta";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import "../styles/info-pages.css";

export default function PricingPage() {
  const currentYear = new Date().getFullYear();

  usePublicPageScroll();
  usePageMeta({
    title: "Pricing - Longhouse",
    description:
      "Self-host Longhouse for free. Hosted availability is by request when you want us to run the always-on Runtime Host.",
  });

  const handleGetStarted = () => {
    window.location.assign("/#landing-install");
  };

  const handleHostedRequest = () => {
    window.location.href = "https://control.longhouse.ai";
  };

  return (
    <div className="info-page">
      <header className="info-page-header">
        <div className="info-page-header-inner">
          <Link to="/" className="info-page-back">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
            Back to Home
          </Link>
          <Link to="/" className="info-page-brand">
            <SwarmLogo size={28} />
            <span className="info-page-brand-name">Longhouse</span>
          </Link>
        </div>
      </header>

      <main className="info-page-content">
        <h1 className="info-page-title">Pricing</h1>
        <p className="info-page-subtitle">
          Self-host first. Hosted later when convenience matters more than running the box yourself.
        </p>

        <div className="pricing-tiers">
          <div className="pricing-tier featured">
            <span className="pricing-tier-badge">Start here</span>
            <h2 className="pricing-tier-name">Self-Hosted</h2>
            <div className="pricing-tier-price">
              Free
            </div>
            <p className="pricing-tier-desc">
              Run Longhouse on your laptop first, then move durability to a box you control when you want it to stay up.
            </p>
            <ul className="pricing-tier-features">
              <li>Free and open source</li>
              <li>Browser, CLI, and <code>/api/agents/*</code> included</li>
              <li>Import Claude Code, Codex CLI, and Gemini CLI sessions immediately</li>
              <li>Durable setup works on a VPS, Mac mini, or homelab box you control</li>
            </ul>
            <Button variant="primary" size="lg" className="pricing-tier-cta" onClick={handleGetStarted}>
              Self-Host Free
            </Button>
          </div>

          <div className="pricing-tier">
            <span className="pricing-tier-badge">By request</span>
            <h2 className="pricing-tier-name">Hosted Later</h2>
            <div className="pricing-tier-price">
              Hosted
            </div>
            <p className="pricing-tier-desc">
              Same session model, with us running the always-on Runtime Host for you.
            </p>
            <ul className="pricing-tier-features">
              <li>Best once you already know you want always-on durability</li>
              <li>Same archive and control loop as self-hosted</li>
              <li>Narrow hosted rollout while launch stays self-host first</li>
              <li>Move over when you want convenience, not because the product requires it</li>
            </ul>
            <Button variant="secondary" size="lg" className="pricing-tier-cta" onClick={handleHostedRequest}>
              Request Hosted Access
            </Button>
          </div>
        </div>

        <div className="pricing-faq">
          <h2>Questions</h2>

          <div className="docs-section">
            <h3>Why is self-host the first path?</h3>
            <p>
              Longhouse is built around sessions running on machines you own. Self-hosting proves the core loop
              directly: import your existing sessions, start one through Longhouse when you want control later,
              and decide on hosting only if you want us to run the durable box.
            </p>

            <h3>What does hosted change?</h3>
            <p>
              Hosted does not unlock a different product. It is the same session archive and machine surface,
              with Longhouse running the always-on Runtime Host for you instead of you running it yourself.
            </p>

            <h3>Where should I start?</h3>
            <p>
              Start on the <Link to="/">home page</Link>, install locally, and get to your first real session.
            </p>

            <h3>Questions?</h3>
            <p>
              Join our <a href="https://discord.gg/h2CWBUrj" target="_blank" rel="noopener noreferrer">Discord</a> or
              email <a href="mailto:support@longhouse.ai">support@longhouse.ai</a>
            </p>
          </div>
        </div>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Longhouse. All rights reserved.</p>
      </footer>
    </div>
  );
}
