import { useEffect } from "react";
import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import "../styles/info-pages.css";

export default function PrivacyPage() {
  const currentYear = new Date().getFullYear();

  useEffect(() => {
    document.title = "Privacy Policy - Swarmlet";
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
        <h1 className="info-page-title">Privacy Policy</h1>
        <p className="info-page-subtitle">
          How we handle your data.
        </p>

        <div className="legal-content">
          <h2>What We Collect</h2>

          <h3>Account Information</h3>
          <p>
            When you sign in with Google, we receive your email address, name, and profile picture
            to identify your account.
          </p>

          <h3>Your Content</h3>
          <p>
            Your agents, workflows, and conversation history are stored to provide the service.
            This data is associated with your account.
          </p>

          <h3>API Keys</h3>
          <p>
            When you add LLM API keys, they are stored encrypted and only used to make API calls on your behalf.
          </p>

          <h2>What We Don't Do</h2>
          <ul>
            <li><strong>We don't train AI on your data.</strong> Your conversations are yours.</li>
            <li><strong>We don't sell your data.</strong></li>
          </ul>

          <h2>Your Rights</h2>
          <p>You can:</p>
          <ul>
            <li><strong>Access</strong> your data through the dashboard</li>
            <li><strong>Delete</strong> your account and data from settings</li>
            <li><strong>Revoke</strong> API keys and integrations at any time</li>
          </ul>

          <h2>Cookies</h2>
          <p>
            We use essential cookies to keep you logged in and remember preferences.
          </p>

          <h2>Third-Party Services</h2>
          <p>We use:</p>
          <ul>
            <li><strong>Google OAuth</strong> for authentication</li>
            <li><strong>Cloud hosting</strong> for infrastructure</li>
          </ul>

          <h2>Contact</h2>
          <p>
            Questions? Email <a href="mailto:privacy@swarmlet.com">privacy@swarmlet.com</a>
          </p>
        </div>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Swarmlet. All rights reserved.</p>
      </footer>
    </div>
  );
}
