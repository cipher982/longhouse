import { useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import "../styles/info-pages.css";

export default function PricingPage() {
  const navigate = useNavigate();
  const currentYear = new Date().getFullYear();

  usePublicPageScroll();

  useEffect(() => {
    document.title = "Pricing - Swarmlet";
    const metaDescription = document.querySelector('meta[name="description"]');
    if (metaDescription) {
      metaDescription.setAttribute('content', 'Free during beta. Full access to Swarmlet\'s AI agent platform while we build and refine the product.');
    }
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
        <h1 className="info-page-title">Pricing</h1>
        <p className="info-page-subtitle">
          Free during beta.
        </p>

        <div className="pricing-tiers">
          <div className="pricing-tier featured">
            <span className="pricing-tier-badge">Current</span>
            <h2 className="pricing-tier-name">Free Beta</h2>
            <div className="pricing-tier-price">
              $0<span>/month</span>
            </div>
            <p className="pricing-tier-desc">
              Full access while we're in beta.
            </p>
            <ul className="pricing-tier-features">
              <li>Create agents and workflows</li>
              <li>Visual workflow builder</li>
              <li>Available integrations</li>
              <li>Powered by OpenAI</li>
            </ul>
            <button className="btn-primary pricing-tier-cta" onClick={handleGetStarted}>
              Get Started Free
            </button>
          </div>
        </div>

        <div className="pricing-faq">
          <h2>Questions</h2>

          <div className="docs-section">
            <h3>Why is it free?</h3>
            <p>
              We're in beta and want early users to help shape the product.
            </p>

            <h3>Questions?</h3>
            <p>
              Join our <a href="https://discord.gg/h2CWBUrj" target="_blank" rel="noopener noreferrer">Discord</a> or
              email <a href="mailto:swarmlet@drose.io">swarmlet@drose.io</a>
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
