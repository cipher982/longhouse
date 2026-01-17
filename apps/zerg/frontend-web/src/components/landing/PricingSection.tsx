/**
 * PricingSection
 *
 * Simple 2-tier pricing: Free Beta + Pro (coming soon)
 */

import config from "../../lib/config";
import { CheckCircleIcon } from "../icons";

interface PricingTier {
  name: string;
  price: string;
  period: string;
  description: string;
  features: string[];
  ctaText: string;
  ctaAction: () => void;
  highlighted?: boolean;
  comingSoon?: boolean;
}

export function PricingSection() {
  const handleGetStarted = () => {
    // If auth is disabled (dev mode), go directly to dashboard
    if (!config.authEnabled) {
      window.location.href = '/dashboard';
      return;
    }
    // Scroll to top and trigger login
    window.scrollTo({ top: 0, behavior: 'smooth' });
    setTimeout(() => {
      document.querySelector<HTMLButtonElement>('.landing-cta-main')?.click();
    }, 500);
  };

  const handleJoinWaitlist = () => {
    window.location.href = 'mailto:swarmlet@drose.io?subject=Pro%20Waitlist&body=I%27d%20like%20to%20join%20the%20waitlist%20for%20Swarmlet%20Pro.';
  };

  const tiers: PricingTier[] = [
    {
      name: "Free Beta",
      price: "$0",
      period: "/month",
      description: "Everything you need to get started",
      features: [
        "5 AI agents",
        "Core integrations",
        "Community support",
        "Basic automations"
      ],
      ctaText: "Start Free Beta",
      ctaAction: handleGetStarted,
      highlighted: true
    },
    {
      name: "Pro",
      price: "$9",
      period: "/month",
      description: "For power users",
      features: [
        "Unlimited agents",
        "All integrations",
        "Priority support",
        "Advanced workflows"
      ],
      ctaText: "Join Waitlist",
      ctaAction: handleJoinWaitlist,
      comingSoon: true
    }
  ];

  return (
    <section id="pricing" className="landing-pricing">
      <div className="landing-section-inner">
        <h2 className="landing-section-title">Simple Pricing</h2>
        <p className="landing-section-subtitle">
          Start free. Upgrade when you need more.
        </p>

        <div className="landing-pricing-grid">
          {tiers.map((tier, index) => (
            <div
              key={index}
              className={`landing-pricing-card ${tier.highlighted ? 'highlighted' : ''} ${tier.comingSoon ? 'coming-soon' : ''}`}
            >
              {tier.comingSoon && (
                <div className="landing-pricing-badge">Coming Soon</div>
              )}
              <div className="landing-pricing-header">
                <h3 className="landing-pricing-name">{tier.name}</h3>
                <div className="landing-pricing-price">
                  <span className="landing-pricing-amount">{tier.price}</span>
                  <span className="landing-pricing-period">{tier.period}</span>
                </div>
                <p className="landing-pricing-description">{tier.description}</p>
              </div>

              <ul className="landing-pricing-features">
                {tier.features.map((feature, featureIndex) => (
                  <li key={featureIndex}>
                    <CheckCircleIcon width={18} height={18} className="landing-pricing-check" />
                    {feature}
                  </li>
                ))}
              </ul>

              <button
                className={`btn-lg landing-pricing-cta ${tier.highlighted ? 'btn-primary' : 'btn-secondary'}`}
                onClick={tier.ctaAction}
              >
                {tier.ctaText}
              </button>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
