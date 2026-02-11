/**
 * PricingSection
 *
 * Simple 2-tier pricing: Self-hosted (free) + Hosted (coming soon).
 *
 * Focuses on the WHAT — features and cost.
 * DeploymentOptions covers the HOW — infrastructure and setup.
 */

import { useState } from "react";
import config from "../../lib/config";
import { CheckCircleIcon } from "../icons";
import { Button } from "../ui";
import { WaitlistModal } from "./WaitlistModal";

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
  const [showWaitlist, setShowWaitlist] = useState(false);

  const handleGetStarted = () => {
    if (config.demoMode || !config.authEnabled) {
      window.location.href = "/timeline";
      return;
    }
    document.querySelector(".install-section")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleJoinWaitlist = () => {
    setShowWaitlist(true);
  };

  const tiers: PricingTier[] = [
    {
      name: "Self-Hosted",
      price: "Free",
      period: "forever",
      description: "Full features, your machine",
      features: [
        "Full timeline and search",
        "SQLite database on your machine",
        "Claude Code + Codex + Gemini sync",
        "Open source (Apache 2.0)",
      ],
      ctaText: "Get Started",
      ctaAction: handleGetStarted,
      highlighted: true,
    },
    {
      name: "Hosted",
      price: "$5",
      period: "/month",
      description: "Always-on, any device",
      features: [
        "Everything in self-hosted",
        "Always-on — agents work while you sleep",
        "Access from any device",
        "Priority support",
      ],
      ctaText: "Join Waitlist",
      ctaAction: handleJoinWaitlist,
      comingSoon: true,
    },
  ];

  return (
    <section id="pricing" className="landing-pricing">
      <div className="landing-section-inner">
        <h2 className="landing-section-title">Simple Pricing</h2>
        <p className="landing-section-subtitle">Self-host free forever. Hosted beta coming soon.</p>

        <div className="landing-pricing-grid">
          {tiers.map((tier, index) => (
            <div
              key={index}
              className={`landing-pricing-card ${tier.highlighted ? "highlighted" : ""} ${tier.comingSoon ? "coming-soon" : ""}`}
            >
              {tier.comingSoon && <div className="landing-pricing-badge">Coming Soon</div>}
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

              <Button
                variant={tier.highlighted ? "primary" : "secondary"}
                size="lg"
                className="landing-pricing-cta"
                onClick={tier.ctaAction}
              >
                {tier.ctaText}
              </Button>
            </div>
          ))}
        </div>
      </div>

      {showWaitlist && <WaitlistModal onClose={() => setShowWaitlist(false)} source="pricing_hosted" />}
    </section>
  );
}
