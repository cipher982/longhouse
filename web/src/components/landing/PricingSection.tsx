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
      description: "Run it on your laptop, VPS, or homelab",
      features: [
        "Full product — timeline, search, live control",
        "SQLite only, no external services needed",
        "Import Claude, Codex, and Gemini sessions",
        "CLI and HTTP API included",
        "Open source (Apache 2.0)",
      ],
      ctaText: "Self-Host Free",
      ctaAction: handleStartFree,
      highlighted: true,
    },
    {
      name: "Hosted Beta",
      price: "$5",
      period: "/month",
      description: "Same product, we run the server",
      features: [
        "Everything in self-hosted",
        "Access from anywhere, no port forwarding",
        "No server to maintain or keep online",
        "Your own subdomain + automatic updates",
        "Migrate from self-hosted anytime",
      ],
      ctaText: "Hosted Beta",
      ctaAction: handleGetHosted,
    },
  ];

  return (
    <section id="pricing" className="landing-pricing">
      <div className="landing-section-inner">
        <p className="landing-section-label">Pricing</p>
        <h2 className="landing-section-title">Free to self-host. Hosted if you want it easy.</h2>
        <p className="landing-section-subtitle">
          Same product either way. Self-host on your own machine, or let us run it for you.
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
