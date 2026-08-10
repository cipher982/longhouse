import { Link } from "react-router-dom";
import { SwarmLogo } from "../SwarmLogo";
import { Button } from "../ui";
import { trackAcquisitionEvent } from "../../lib/analytics";

export function FooterCTA() {
  const handleDownload = () => {
    trackAcquisitionEvent("mac_download_click", {
      surface: "landing",
      placement: "footer",
      method: "direct_download",
    });
    window.location.assign("/download/macos");
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
        <div className="landing-footer-cta">
          <h2 className="landing-footer-quote">Start steering the agents you already run.</h2>
          <p className="landing-footer-cta-copy">
            Install on macOS, or from the shell on Linux and WSL. Point it at your
            existing CLIs and your sessions show up.
          </p>
          <div className="landing-footer-cta-buttons">
            <Button variant="primary" size="lg" onClick={handleDownload}>
              Download for macOS
            </Button>
            <Button variant="secondary" size="lg" onClick={handleDocs}>
              Read the docs
            </Button>
          </div>
          <p className="landing-footer-subnote">
            Hosted is $5 per month.{" "}
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
              Create a hosted account
            </a>.
          </p>
        </div>

        <div className="landing-footer-links">
          <a href="/" className="landing-footer-brand">
            <SwarmLogo size={32} />
            <span className="landing-footer-name">Longhouse</span>
          </a>

          <nav className="landing-footer-nav" aria-label="Footer">
            <Link to="/docs">Docs</Link>
            <a href="https://github.com/cipher982/longhouse" target="_blank" rel="noopener noreferrer">GitHub</a>
            <Link to="/changelog">Changelog</Link>
            <Link to="/security">Security</Link>
            <Link to="/privacy">Privacy</Link>
            <a href="mailto:support@longhouse.ai">Contact</a>
          </nav>
        </div>

        <div className="landing-footer-bottom">
          <p>© {currentYear} Longhouse · Apache-2.0</p>
        </div>
      </div>
    </footer>
  );
}
