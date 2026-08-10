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
      description: "Run the server on hardware you already own.",
      features: [
        "Start on your laptop, move it to a Mac mini, home server, or VPS later",
        "Your archive is a SQLite file on your disk",
        "Web UI, CLI, iPhone app, and machine API all included",
        "Apache-2.0, no account required",
      ],
      ctaText: "Download for macOS",
      ctaAction: handleStartFree,
      highlighted: true,
    },
    {
      name: "Hosted",
      callout: "$5/month",
      description: "We run a private server for you.",
      features: [
        "Nothing to install, patch, or expose to the internet",
        "Your own address on longhouse.ai",
        "Your history and timeline stay reachable when your laptop is asleep",
        "Same Mac, Linux, and iPhone clients",
      ],
      ctaText: "Get hosted",
      ctaAction: handleGetHosted,
    },
  ];

  return (
    <section id="pricing" className="landing-pricing">
      <div className="landing-section-inner">
        <h2 className="landing-pricing-heading">
          The server is yours to run, or ours.
        </h2>
        <p className="landing-pricing-subhead">
          Longhouse&rsquo;s server holds your session archive and serves the web UI. Run it
          on your own hardware for free, or pay $5 a month and we run a private one for
          you. Either way the agents run on your machines.
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
