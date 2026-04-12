import { CheckCircleIcon } from "../icons";
import { Button } from "../ui";

interface PricingTier {
  label: string;
  name: string;
  callout: string;
  description: string;
  features: string[];
  ctaText: string;
  ctaAction: () => void;
  highlighted?: boolean;
}

export function PricingSection() {
  const handleStartFree = () => {
    document.getElementById("landing-install")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleRequestHosted = () => {
    window.location.href = "https://control.longhouse.ai";
  };

  const tiers: PricingTier[] = [
    {
      label: "Start here",
      name: "Self-Hosted",
      callout: "Free",
      description: "Put Longhouse where durability should live and get first value without signing up first.",
      features: [
        "Run it on your laptop first, or on a VPS / Mac mini / homelab box for durability",
        "Free and open source",
        "Import existing Claude, Codex, and Gemini sessions immediately",
        "Browser, CLI, and /api/agents/* included",
      ],
      ctaText: "Self-Host Free",
      ctaAction: handleStartFree,
      highlighted: true,
    },
    {
      label: "Convenience path",
      name: "Hosted Later",
      callout: "By request",
      description: "Same session model, with us running the always-on Runtime Host for you.",
      features: [
        "Same archive and control loop as self-hosted",
        "Useful when you already want durability but do not want to run the box",
        "A narrow beta while the launch story stays self-host first",
        "Migrate from self-hosted when you want us to run the always-on machine",
      ],
      ctaText: "Request Hosted Beta",
      ctaAction: handleRequestHosted,
    },
  ];

  return (
    <section id="pricing" className="landing-pricing">
      <div className="landing-section-inner">
        <p className="landing-section-label">Deployment</p>
        <h2 className="landing-section-title">Self-host first. Hosted later when you want convenience.</h2>
        <p className="landing-section-subtitle">
          Hosted is the same product with us running the always-on box. It is not the thing that unlocks
          the core loop.
        </p>

        <div className="landing-pricing-grid">
          {tiers.map((tier, index) => (
            <div
              key={index}
              className={`landing-pricing-card ${tier.highlighted ? "highlighted" : ""}`}
            >
              <div className="landing-pricing-header">
                <div className="landing-pricing-badge">{tier.label}</div>
                <h3 className="landing-pricing-name">{tier.name}</h3>
                <p className="landing-pricing-callout">{tier.callout}</p>
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
