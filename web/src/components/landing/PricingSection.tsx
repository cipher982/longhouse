/**
 * PricingSection
 *
 * Simple 2-tier pricing: Self-hosted (free) + Hosted (coming soon).
 * Covers both deployment model and pricing in one section.
 */

import { CheckCircleIcon } from "../icons";
import { Button } from "../ui";

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
    document.querySelector(".install-section")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleGetHosted = () => {
    window.location.href = "https://control.longhouse.ai";
  };

  const tiers: PricingTier[] = [
    {
      name: "Self-Hosted",
      price: "Free",
      period: "forever",
      description: "Run it on your own machine",
      features: [
        "Full timeline and search",
        "SQLite — no external services",
        "Claude Code + Codex + Gemini sync",
        "Works offline, data stays local",
        "Open source (Apache 2.0)",
      ],
      ctaText: "Self-host Free",
      ctaAction: handleGetStarted,
    },
    {
      name: "Hosted",
      price: "$5",
      period: "/month",
      description: "Always-on agents, from anywhere",
      features: [
        "Everything in self-hosted",
        "Agents keep running when you close your laptop",
        "Resume sessions from any device",
        "Inter-agent communication across sessions",
        "Your own subdomain + automatic updates",
      ],
      ctaText: "Get Started",
      ctaAction: handleGetHosted,
      highlighted: true,
    },
  ];

  return (
    <section id="pricing" className="landing-pricing">
      <div className="landing-section-inner">
        <h2 className="landing-section-title">Simple Pricing</h2>
        <p className="landing-section-subtitle">Always-on agents for $5/mo. Or self-host free.</p>

        <div className="landing-pricing-grid">
          {tiers.map((tier, index) => (
            <div
              key={index}
              className={`landing-pricing-card ${tier.highlighted ? "highlighted" : ""} ${tier.comingSoon ? "coming-soon" : ""}`}
            >
              {tier.comingSoon && <div className="landing-pricing-badge">Beta</div>}
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

    </section>
  );
}
