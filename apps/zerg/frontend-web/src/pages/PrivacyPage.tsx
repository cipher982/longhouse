import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import "../styles/info-pages.css";

export default function PrivacyPage() {
  const currentYear = new Date().getFullYear();
  const lastUpdated = "December 2, 2024";

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
        <p className="info-page-updated">Last updated: {lastUpdated}</p>

        <div className="legal-content">
          <p>
            At Swarmlet, we take your privacy seriously. This policy describes what data we collect,
            how we use it, and your rights regarding your information.
          </p>

          <h2>Information We Collect</h2>

          <h3>Account Information</h3>
          <p>
            When you sign up, we collect your email address and basic profile information from your
            Google account (name, profile picture). This is used to identify you and provide the service.
          </p>

          <h3>Usage Data</h3>
          <p>We collect anonymous usage data to improve our service, including:</p>
          <ul>
            <li>Pages visited and features used</li>
            <li>Agent and workflow execution counts</li>
            <li>Error reports and performance metrics</li>
            <li>Device type and browser information</li>
          </ul>

          <h3>Agent Data</h3>
          <p>
            Your agents, workflows, and conversation history are stored to provide the service.
            This data is associated with your account and accessible only to you.
          </p>

          <h3>API Keys</h3>
          <p>
            When you add integration credentials or LLM API keys, they are encrypted at rest
            and never logged or exposed. See our <Link to="/security">Security</Link> page for details.
          </p>

          <h2>How We Use Your Data</h2>
          <p>We use your data to:</p>
          <ul>
            <li>Provide and maintain the Swarmlet service</li>
            <li>Authenticate you and secure your account</li>
            <li>Send service-related communications</li>
            <li>Improve and optimize the platform</li>
            <li>Respond to support requests</li>
          </ul>

          <h2>Data We Don't Collect</h2>
          <p>We want to be clear about what we don't do:</p>
          <ul>
            <li><strong>We don't train AI models on your data.</strong> Your conversations and workflows are yours.</li>
            <li><strong>We don't sell your data.</strong> Ever.</li>
            <li><strong>We don't share data with third parties</strong> except as needed to provide the service (e.g., cloud infrastructure).</li>
          </ul>

          <h2>Data Storage & Retention</h2>

          <h3>Where We Store Data</h3>
          <p>
            Your data is stored on secure servers hosted in the United States.
            We use industry-standard encryption for data in transit and at rest.
          </p>

          <h3>How Long We Keep Data</h3>
          <p>
            We retain your data for as long as your account is active. When you delete your account,
            we delete your personal data within 30 days, except where retention is required by law.
          </p>

          <h2>Your Rights</h2>
          <p>You have the right to:</p>
          <ul>
            <li><strong>Access</strong> your data - Export your agents, workflows, and conversations</li>
            <li><strong>Correct</strong> your data - Update your profile information anytime</li>
            <li><strong>Delete</strong> your data - Delete your account and all associated data</li>
            <li><strong>Object</strong> to processing - Contact us to opt out of analytics</li>
          </ul>

          <h2>Cookies</h2>
          <p>
            We use essential cookies to keep you logged in and remember your preferences.
            We use analytics cookies (with your consent where required) to understand how the
            service is used. You can disable cookies in your browser, but some features may not work.
          </p>

          <h2>Third-Party Services</h2>
          <p>We use the following third-party services:</p>
          <ul>
            <li><strong>Google OAuth</strong> - For authentication</li>
            <li><strong>Cloud infrastructure</strong> - For hosting and data storage</li>
            <li><strong>Analytics</strong> - For anonymous usage tracking</li>
          </ul>
          <p>
            Each of these providers has their own privacy policy governing their use of your data.
          </p>

          <h2>Children's Privacy</h2>
          <p>
            Swarmlet is not intended for users under 13 years of age. We do not knowingly collect
            data from children under 13. If you believe we have collected such data, please contact us.
          </p>

          <h2>Changes to This Policy</h2>
          <p>
            We may update this policy from time to time. We'll notify you of significant changes
            via email or in-app notification. The "Last updated" date at the top indicates when
            the policy was last revised.
          </p>

          <h2>Contact Us</h2>
          <p>
            If you have questions about this policy or your data, contact us at:
          </p>
          <p>
            <a href="mailto:privacy@swarmlet.ai">privacy@swarmlet.ai</a>
          </p>
        </div>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Swarmlet. All rights reserved.</p>
      </footer>
    </div>
  );
}
