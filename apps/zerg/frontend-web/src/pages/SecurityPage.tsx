import { useEffect } from "react";
import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { ShieldIcon, LockIcon, TrashIcon, KeyIcon } from "../components/icons";
import "../styles/info-pages.css";

export default function SecurityPage() {
  const currentYear = new Date().getFullYear();

  useEffect(() => {
    document.title = "Security - Swarmlet";
  }, []);

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
            <span className="info-page-brand-name">Swarmlet</span>
          </Link>
        </div>
      </header>

      <main className="info-page-content">
        <h1 className="info-page-title">Security</h1>
        <p className="info-page-subtitle">
          How we approach security and protect your data.
        </p>

        <div className="security-highlights">
          <div className="security-highlight">
            <div className="security-highlight-icon">
              <ShieldIcon width={24} height={24} />
            </div>
            <h3>HTTPS Only</h3>
            <p>All connections use HTTPS</p>
          </div>
          <div className="security-highlight">
            <div className="security-highlight-icon">
              <LockIcon width={24} height={24} />
            </div>
            <h3>OAuth 2.0</h3>
            <p>Secure Google authentication</p>
          </div>
          <div className="security-highlight">
            <div className="security-highlight-icon">
              <KeyIcon width={24} height={24} />
            </div>
            <h3>Secure Credentials</h3>
            <p>Integration credentials encrypted</p>
          </div>
          <div className="security-highlight">
            <div className="security-highlight-icon">
              <TrashIcon width={24} height={24} />
            </div>
            <h3>Data Deletion</h3>
            <p>Delete your account anytime</p>
          </div>
        </div>

        <div className="legal-content">
          <h2>Authentication</h2>
          <p>
            Swarmlet uses Google OAuth 2.0 for authentication. We never see or store your Google password.
            You can revoke access anytime from your Google account settings.
          </p>

          <h2>Integration Credentials</h2>
          <p>
            When you connect integrations (Slack, Discord, GitHub, etc.), your credentials are stored
            encrypted and only used to connect to those services on your behalf.
          </p>

          <h2>Your Controls</h2>
          <p>You have control over your data:</p>
          <ul>
            <li><strong>View</strong> - See your agents and workflows in the dashboard</li>
            <li><strong>Delete</strong> - Remove your account and data from settings</li>
            <li><strong>Revoke</strong> - Disconnect integrations and delete credentials</li>
          </ul>

          <h2>Responsible Disclosure</h2>
          <p>
            If you discover a security vulnerability, please report it to us:
          </p>
          <ul>
            <li>Email: <a href="mailto:security@swarmlet.com">security@swarmlet.com</a></li>
            <li>Include details and steps to reproduce</li>
            <li>Allow reasonable time for us to address it</li>
          </ul>

          <div className="security-contact">
            <h3>Questions?</h3>
            <p>
              For security questions, email{" "}
              <a href="mailto:security@swarmlet.com">security@swarmlet.com</a>
            </p>
            <p>
              For privacy questions, see our <Link to="/privacy">Privacy Policy</Link>.
            </p>
          </div>
        </div>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Swarmlet. All rights reserved.</p>
      </footer>
    </div>
  );
}
