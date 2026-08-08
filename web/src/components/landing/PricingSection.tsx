import { CheckCircleIcon } from "../icons";
import { Button } from "../ui";
import { trackAcquisitionEvent } from "../../lib/analytics";

interface PricingTier {
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
      name: "Self-Hosted",
      callout: "Free",
      description: "Run the Longhouse server on hardware you control.",
      features: [
        "Try it on a laptop, then move it to a Mac mini, home server, or VPS",
        "SQLite archive under your control",
        "Browser, CLI, and machine API included",
        "Apache-2.0 licensed",
      ],
      ctaText: "Download for macOS",
      ctaAction: handleStartFree,
      highlighted: true,
    },
    {
      name: "Hosted",
      callout: "$5/month",
      description: "We run a private Longhouse server for you.",
      features: [
        "No server setup or maintenance",
        "A private address on longhouse.ai",
        "Your timeline stays available when dev machines are offline",
        "Use the same Mac, Linux, and iPhone clients",
      ],
      ctaText: "Get hosted",
      ctaAction: handleGetHosted,
    },
  ];

  return (
    <section id="pricing" className="landing-pricing">
      <div className="landing-section-inner">
        <h2 className="landing-pricing-heading">
          Choose where the Longhouse server runs.
        </h2>
        <p className="landing-pricing-subhead">
          The server stores your archive and serves the web UI. Run it yourself for free,
          or use hosted for $5 per month.
        </p>

        <div className="landing-pricing-grid">
          {tiers.map((tier, index) => (
            <div
              key={index}
              className={`landing-pricing-card ${tier.highlighted ? "highlighted" : ""}`}
            >
              <div className="landing-pricing-header">
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
