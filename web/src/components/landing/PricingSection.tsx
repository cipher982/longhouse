import { CheckCircleIcon } from "../icons";
import { Button } from "../ui";
import { trackAcquisitionEvent } from "../../lib/analytics";

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
    trackAcquisitionEvent("self_host_cta_click", {
      surface: "landing",
      placement: "pricing",
    });
    document.getElementById("landing-install")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleGetHosted = () => {
    trackAcquisitionEvent("hosted_signup_click", {
      surface: "landing",
      placement: "pricing",
      plan: "hosted_5",
    });
    window.location.href = "https://control.longhouse.ai/signup";
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
        "Import existing Claude, Codex, Antigravity, OpenCode, and legacy Gemini sessions immediately",
        "Browser, CLI, and /api/agents/* included",
      ],
      ctaText: "Self-Host Free",
      ctaAction: handleStartFree,
      highlighted: true,
    },
    {
      label: "We run it for you",
      name: "Hosted",
      callout: "$5 / month",
      description: "Same session model, with us running the always-on Runtime Host for you.",
      features: [
        "Same archive and control loop as self-hosted",
        "Skip running the box — we keep it up",
        "Your own subdomain on longhouse.ai",
        "Migrate from self-hosted any time",
      ],
      ctaText: "Get Hosted · $5/mo",
      ctaAction: handleGetHosted,
    },
  ];

  return (
    <section id="pricing" className="landing-pricing">
      <div className="landing-section-inner">
        <p className="landing-section-label">Deployment</p>
        <h2 className="landing-section-title">Two ways in.</h2>
        <p className="landing-section-subtitle">
          Run it yourself for free, or pay $5/month and we run it for you. Same product either way.
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
