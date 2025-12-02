import { useEffect } from "react";
import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { ShieldIcon, LockIcon, EyeIcon, TrashIcon, ServerIcon, KeyIcon } from "../components/icons";
import "../styles/info-pages.css";

export default function SecurityPage() {
  const currentYear = new Date().getFullYear();
  const lastUpdated = "December 2, 2024";

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
        <h1 className="info-page-title">Security & Trust Center</h1>
        <p className="info-page-subtitle">
          How we protect your data, secure your account, and earn your trust.
        </p>
        <p className="info-page-updated">Last updated: {lastUpdated}</p>

        <div className="security-highlights">
          <div className="security-highlight">
            <div className="security-highlight-icon">
              <LockIcon width={24} height={24} />
            </div>
            <h3>Encrypted at Rest</h3>
            <p>All data encrypted with AES-256</p>
          </div>
          <div className="security-highlight">
            <div className="security-highlight-icon">
              <ShieldIcon width={24} height={24} />
            </div>
            <h3>Encrypted in Transit</h3>
            <p>TLS 1.3 for all connections</p>
          </div>
          <div className="security-highlight">
            <div className="security-highlight-icon">
              <KeyIcon width={24} height={24} />
            </div>
            <h3>Secure Credentials</h3>
            <p>API keys encrypted, never logged</p>
          </div>
          <div className="security-highlight">
            <div className="security-highlight-icon">
              <TrashIcon width={24} height={24} />
            </div>
            <h3>Data Deletion</h3>
            <p>Full account deletion available</p>
          </div>
        </div>

        <div className="legal-content">
          <h2>Authentication</h2>
          <p>
            Swarmlet uses Google OAuth 2.0 for authentication. This means:
          </p>
          <ul>
            <li>We never see or store your Google password</li>
            <li>You can revoke access anytime from your Google account settings</li>
            <li>We receive minimal profile data (email, name, picture)</li>
            <li>Session tokens are short-lived and refreshed automatically</li>
          </ul>

          <div className="security-architecture">
            <h3>Architecture Overview</h3>
            <p>
              Swarmlet runs on a modern, secure infrastructure:
            </p>
            <ul>
              <li><strong>Frontend</strong> - React SPA served over HTTPS</li>
              <li><strong>API</strong> - FastAPI backend with JWT authentication</li>
              <li><strong>Database</strong> - PostgreSQL with encrypted connections</li>
              <li><strong>Real-time</strong> - WebSocket with token authentication</li>
              <li><strong>Secrets</strong> - Encrypted credential storage, separate from app data</li>
            </ul>
          </div>

          <h2>Your API Keys & Credentials</h2>
          <p>
            When you add LLM API keys or integration credentials, they receive special handling:
          </p>
          <ul>
            <li>Encrypted using AES-256 before storage</li>
            <li>Stored in a separate, access-controlled database</li>
            <li>Never written to logs or error reports</li>
            <li>Only decrypted in memory when actively used</li>
            <li>Immediately deletable from your settings</li>
          </ul>

          <h2>What We Log</h2>
          <p>
            For debugging and security monitoring, we log:
          </p>
          <ul>
            <li>API request timestamps and response codes (not bodies)</li>
            <li>Authentication events (login, logout)</li>
            <li>Error stack traces (sanitized of sensitive data)</li>
            <li>Rate limiting events</li>
          </ul>
          <p>
            We explicitly <strong>do not log</strong>:
          </p>
          <ul>
            <li>API keys or credentials</li>
            <li>Conversation content</li>
            <li>Full request/response bodies</li>
          </ul>

          <h2>Data Location</h2>
          <p>
            Your data is stored on servers in the United States. We use reputable cloud
            providers with SOC 2 Type II certifications. Data is not transferred outside
            the US unless you configure an integration that does so.
          </p>

          <h2>Your Controls</h2>
          <p>
            You have full control over your data:
          </p>
          <ul>
            <li><strong>View</strong> - See all data associated with your account</li>
            <li><strong>Export</strong> - Download your agents, workflows, and history</li>
            <li><strong>Delete</strong> - Remove your entire account and all data</li>
            <li><strong>Revoke</strong> - Disconnect integrations and delete credentials instantly</li>
          </ul>
          <p>
            Account deletion is available in Profile &gt; Settings. Deletion is permanent
            and completed within 30 days.
          </p>

          <div className="security-roadmap">
            <h3>Compliance Roadmap</h3>
            <p>We're committed to meeting industry security standards:</p>

            <div className="security-roadmap-item">
              <span className="security-roadmap-status done">Done</span>
              <div>
                <strong>HTTPS Everywhere</strong> - All traffic encrypted with TLS 1.3
              </div>
            </div>

            <div className="security-roadmap-item">
              <span className="security-roadmap-status done">Done</span>
              <div>
                <strong>OAuth 2.0</strong> - Secure authentication via Google
              </div>
            </div>

            <div className="security-roadmap-item">
              <span className="security-roadmap-status done">Done</span>
              <div>
                <strong>Encrypted Credentials</strong> - AES-256 for stored secrets
              </div>
            </div>

            <div className="security-roadmap-item">
              <span className="security-roadmap-status in-progress">In Progress</span>
              <div>
                <strong>Penetration Testing</strong> - Third-party security audit
              </div>
            </div>

            <div className="security-roadmap-item">
              <span className="security-roadmap-status planned">Planned</span>
              <div>
                <strong>SOC 2 Type I</strong> - Compliance certification
              </div>
            </div>

            <div className="security-roadmap-item">
              <span className="security-roadmap-status planned">Planned</span>
              <div>
                <strong>GDPR Compliance</strong> - Full European data protection
              </div>
            </div>
          </div>

          <h2>Responsible Disclosure</h2>
          <p>
            If you discover a security vulnerability, please report it responsibly:
          </p>
          <ul>
            <li>Email: <a href="mailto:security@swarmlet.ai">security@swarmlet.ai</a></li>
            <li>Include details of the vulnerability and steps to reproduce</li>
            <li>Give us reasonable time to address it before public disclosure</li>
          </ul>
          <p>
            We appreciate security researchers and will acknowledge your contribution
            (with your permission) once the issue is resolved.
          </p>

          <div className="security-contact">
            <h3>Questions?</h3>
            <p>
              For security-related inquiries, contact us at{" "}
              <a href="mailto:security@swarmlet.ai">security@swarmlet.ai</a>
            </p>
            <p>
              For general privacy questions, see our <Link to="/privacy">Privacy Policy</Link> or
              email <a href="mailto:privacy@swarmlet.ai">privacy@swarmlet.ai</a>
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
