import { Link } from "react-router-dom";
import { SwarmLogo } from "../SwarmLogo";
import { Button } from "../ui";
import config from "../../lib/config";

export function FooterCTA() {
  const handleStartFree = () => {
    if (config.marketingOnly) {
      document.querySelector(".install-section")?.scrollIntoView({ behavior: "smooth" });
      return;
    }
    // If auth is disabled (dev mode), go directly to timeline
    if (!config.authEnabled) {
      window.location.href = '/timeline';
      return;
    }
    window.scrollTo({ top: 0, behavior: 'smooth' });
    setTimeout(() => {
      document.querySelector<HTMLButtonElement>('.landing-cta-main')?.click();
    }, 500);
  };

  const currentYear = new Date().getFullYear();

  return (
    <footer className="landing-footer">
      <div className="landing-section-inner">
        {/* Final CTA */}
        <div className="landing-footer-cta">
          <blockquote className="landing-footer-quote">
            Your cloud workspace is waiting.
          </blockquote>
          <Button variant="primary" size="lg" className="landing-cta-main" onClick={handleStartFree}>
            Get your Longhouse 🪵
          </Button>
        </div>

        {/* Footer links */}
        <div className="landing-footer-links">
          <div className="landing-footer-brand">
            <SwarmLogo size={32} />
            <span className="landing-footer-name">Longhouse</span>
          </div>

          <nav className="landing-footer-nav">
            <div className="landing-footer-nav-group">
              <h4>Product</h4>
              <a href="#how-it-works">How It Works</a>
              <a href="#integrations">Integrations</a>
              <a href="#pricing">Pricing</a>
            </div>
            <div className="landing-footer-nav-group">
              <h4>Resources</h4>
              <Link to="/docs">Documentation</Link>
              <Link to="/changelog">Changelog</Link>
              <a href="https://github.com/cipher982/longhouse" target="_blank" rel="noopener noreferrer">GitHub</a>
            </div>
            <div className="landing-footer-nav-group">
              <h4>Company</h4>
              <Link to="/security">Security</Link>
              <Link to="/privacy">Privacy</Link>
              <a href="mailto:support@longhouse.ai">Contact</a>
              <a href="https://discord.gg/h2CWBUrj" target="_blank" rel="noopener noreferrer">Discord</a>
            </div>
          </nav>
        </div>

        <div className="landing-footer-bottom">
          <p>© {currentYear} Longhouse. All rights reserved.</p>
        </div>
      </div>
    </footer>
  );
}
