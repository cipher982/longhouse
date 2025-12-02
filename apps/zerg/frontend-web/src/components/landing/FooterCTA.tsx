import { Link } from "react-router-dom";
import { SwarmLogo } from "../SwarmLogo";

export function FooterCTA() {
  const handleStartFree = () => {
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
            Life is noisy. You deserve a brain that pays attention for you.
          </blockquote>
          <button className="btn-primary btn-lg landing-cta-main" onClick={handleStartFree}>
            Start Free
          </button>
        </div>

        {/* Footer links */}
        <div className="landing-footer-links">
          <div className="landing-footer-brand">
            <SwarmLogo size={32} />
            <span className="landing-footer-name">Swarmlet</span>
          </div>
          
          <nav className="landing-footer-nav">
            <div className="landing-footer-nav-group">
              <h4>Product</h4>
              <a href="#scenarios">Features</a>
              <a href="#integrations">Integrations</a>
              <Link to="/pricing">Pricing</Link>
            </div>
            <div className="landing-footer-nav-group">
              <h4>Resources</h4>
              <Link to="/docs">Documentation</Link>
              <Link to="/changelog">Changelog</Link>
              <a href="https://github.com/swarmlet" target="_blank" rel="noopener noreferrer">GitHub</a>
            </div>
            <div className="landing-footer-nav-group">
              <h4>Company</h4>
              <Link to="/security">Security</Link>
              <Link to="/privacy">Privacy</Link>
              <a href="mailto:swarmlet@drose.io">Contact</a>
              <a href="https://discord.gg/h2CWBUrj" target="_blank" rel="noopener noreferrer">Discord</a>
            </div>
          </nav>
        </div>

        <div className="landing-footer-bottom">
          <p>© {currentYear} Swarmlet. All rights reserved.</p>
        </div>
      </div>
    </footer>
  );
}

