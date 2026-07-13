import { Link } from "react-router-dom";
import { SwarmLogo } from "../SwarmLogo";
import { Button } from "../ui";
import { trackAcquisitionEvent } from "../../lib/analytics";

export function FooterCTA() {
  const handleSelfHost = () => {
    trackAcquisitionEvent("self_host_cta_click", {
      surface: "landing",
      placement: "footer",
    });
    document.getElementById("landing-install")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleDocs = () => {
    trackAcquisitionEvent("docs_click", {
      surface: "landing",
      placement: "footer",
    });
    window.location.assign("/docs");
  };

  const currentYear = new Date().getFullYear();

  return (
    <footer className="landing-footer">
      <div className="landing-section-inner">
        {/* Final CTA */}
        <div className="landing-footer-cta">
          <blockquote className="landing-footer-quote">
            Launch it. Walk away. Steer it from anywhere.
          </blockquote>
          <div className="landing-footer-cta-buttons">
            <Button variant="primary" size="lg" onClick={handleSelfHost}>
              Self-Host Free
            </Button>
          </div>
          <p className="landing-footer-subnote">
            Or skip running the box —{" "}
            <a
              href="https://control.longhouse.ai/signup"
              onClick={() =>
                trackAcquisitionEvent("hosted_signup_click", {
                  surface: "landing",
                  placement: "footer",
                  plan: "hosted_5",
                })
              }
            >
              get hosted for $5/mo
            </a>.{" "}
            <a href="/docs" className="landing-footer-subnote-docs" onClick={handleDocs}>Read the docs</a>.
          </p>
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
              <a href="#how-it-works">How it works</a>
              <a href="#surface">CLI & API</a>
              <a href="#providers">Providers</a>
              <a href="#pricing">Deployment</a>
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
              <a href="https://discord.gg/mekG4Pp5q" target="_blank" rel="noopener noreferrer">Discord</a>
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
