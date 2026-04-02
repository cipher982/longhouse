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
  const handleStartFree = () => {
    document.getElementById("landing-install")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleGetHosted = () => {
    window.location.href = "https://control.longhouse.ai";
  };

  const tiers: PricingTier[] = [
    {
      name: "Self-Hosted",
      price: "Free",
      period: "forever",
      description: "The proof-of-value path",
      features: [
        "Local install + demo path",
        "SQLite core, no external services",
        "Claude Code + Codex + Gemini archive sync",
        "Search, detail, and machine surface",
        "Open source (Apache 2.0)",
      ],
      ctaText: "Start Free Locally",
      ctaAction: handleStartFree,
      highlighted: true,
    },
    {
      name: "Hosted Beta",
      price: "$5",
      period: "/month",
      description: "The always-on upgrade",
      features: [
        "Everything in self-hosted",
        "Always-on browser access from anywhere",
        "Managed cloud sessions",
        "Your own subdomain + automatic updates",
        "Less operator work",
      ],
      ctaText: "Join Hosted Beta",
      ctaAction: handleGetHosted,
    },
  ];

  return (
    <section id="pricing" className="landing-pricing">
      <div className="landing-section-inner">
        <p className="landing-section-label">Free First</p>
        <h2 className="landing-section-title">Charge for always-on, not for understanding the product.</h2>
        <p className="landing-section-subtitle">
          The first win should happen locally. Hosted is the paid convenience layer when you already believe.
        </p>

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
