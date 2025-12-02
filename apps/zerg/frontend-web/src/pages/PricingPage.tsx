import { useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import "../styles/info-pages.css";

export default function PricingPage() {
  const navigate = useNavigate();
  const currentYear = new Date().getFullYear();

  useEffect(() => {
    document.title = "Pricing - Swarmlet";
  }, []);

  const handleGetStarted = () => {
    navigate("/");
    setTimeout(() => {
      document.querySelector<HTMLButtonElement>(".landing-cta-main")?.click();
    }, 100);
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
            <span className="info-page-brand-name">Swarmlet</span>
          </Link>
        </div>
      </header>

      <main className="info-page-content">
        <h1 className="info-page-title">Simple, Transparent Pricing</h1>
        <p className="info-page-subtitle">
          Start free, scale as you grow. No hidden fees, no surprises.
        </p>

        <div className="pricing-tiers">
          {/* Free Tier */}
          <div className="pricing-tier featured">
            <span className="pricing-tier-badge">Current</span>
            <h2 className="pricing-tier-name">Free Beta</h2>
            <div className="pricing-tier-price">
              $0<span>/month</span>
            </div>
            <p className="pricing-tier-desc">
              Full access during our beta period. Help shape the product.
            </p>
            <ul className="pricing-tier-features">
              <li>Unlimited agents</li>
              <li>Visual workflow builder</li>
              <li>All integrations</li>
              <li>Bring your own LLM keys</li>
              <li>Community support</li>
              <li>Early adopter perks</li>
            </ul>
            <button className="btn-primary pricing-tier-cta" onClick={handleGetStarted}>
              Get Started Free
            </button>
          </div>

          {/* Pro Tier */}
          <div className="pricing-tier">
            <h2 className="pricing-tier-name">Pro</h2>
            <div className="pricing-tier-price">
              Coming Soon
            </div>
            <p className="pricing-tier-desc">
              For power users who want more capacity and support.
            </p>
            <ul className="pricing-tier-features">
              <li>Everything in Free</li>
              <li>Higher rate limits</li>
              <li>Priority support</li>
              <li>Advanced analytics</li>
              <li>Team collaboration</li>
              <li>Custom integrations</li>
            </ul>
            <button className="btn-secondary pricing-tier-cta" disabled>
              Join Waitlist
            </button>
          </div>

          {/* Enterprise Tier */}
          <div className="pricing-tier">
            <h2 className="pricing-tier-name">Enterprise</h2>
            <div className="pricing-tier-price">
              Contact Us
            </div>
            <p className="pricing-tier-desc">
              For organizations with custom requirements and scale.
            </p>
            <ul className="pricing-tier-features">
              <li>Everything in Pro</li>
              <li>Dedicated infrastructure</li>
              <li>SSO & advanced security</li>
              <li>SLA guarantees</li>
              <li>Dedicated support</li>
              <li>Custom development</li>
            </ul>
            <a href="mailto:hello@swarmlet.ai" className="btn-secondary pricing-tier-cta" style={{ display: 'block', textAlign: 'center', textDecoration: 'none' }}>
              Contact Sales
            </a>
          </div>
        </div>

        <div className="pricing-faq">
          <h2>Frequently Asked Questions</h2>

          <div className="docs-section">
            <h3>Why is it free?</h3>
            <p>
              We're in beta and want to get feedback from real users. You bring your own LLM API keys,
              so our costs are minimal. This lets us focus on building the best product possible.
            </p>

            <h3>Will you always have a free tier?</h3>
            <p>
              Yes. We believe everyone should have access to AI automation. The free tier will always
              include core functionality. Paid tiers will offer additional capacity and features.
            </p>

            <h3>What happens to my data during beta?</h3>
            <p>
              Your data is yours. We don't train on your data. If we ever sunset the beta, we'll give
              you ample notice and export tools. See our <Link to="/privacy">Privacy Policy</Link> for details.
            </p>

            <h3>Do I need to provide my own API keys?</h3>
            <p>
              Yes, you bring your own OpenAI, Anthropic, or other LLM API keys. This means you have full
              control over your AI spend and aren't locked into our pricing model.
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
