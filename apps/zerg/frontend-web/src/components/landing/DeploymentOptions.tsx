/**
 * DeploymentOptions
 *
 * Two-card section showing deployment paths:
 * Self-hosted (primary) and Hosted Beta.
 *
 * Focuses on the HOW — infrastructure and setup method.
 * PricingSection covers the WHAT — features and cost.
 */

import { useState } from "react";
import { Button } from "../ui";
import { CheckCircleIcon } from "../icons";
import { WaitlistModal } from "./WaitlistModal";

interface DeploymentOption {
  name: string;
  promise: string;
  features: string[];
  ctaText: string;
  ctaAction: () => void;
  ctaVariant: "primary" | "secondary";
  highlighted?: boolean;
  badge?: string;
}

export function DeploymentOptions() {
  const [showWaitlist, setShowWaitlist] = useState(false);

  const handleInstallCLI = () => {
    document.querySelector(".install-section")?.scrollIntoView({ behavior: "smooth" });
  };

  const handleJoinWaitlist = () => {
    setShowWaitlist(true);
  };

  const options: DeploymentOption[] = [
    {
      name: "Self-hosted",
      promise: "One command, your machine",
      features: [
        "SQLite — no external services",
        "Runs on any Mac, Linux, or VPS",
        "Works offline, data stays local",
        "Open source (Apache 2.0)",
      ],
      ctaText: "Self-Host Now",
      ctaAction: handleInstallCLI,
      ctaVariant: "primary",
      highlighted: true,
    },
    {
      name: "Hosted Beta",
      promise: "We run it, you use it",
      features: [
        "Always-on — agents keep working",
        "Access from any device",
        "Automatic updates and backups",
        "Your own subdomain",
      ],
      ctaText: "Join Waitlist",
      ctaAction: handleJoinWaitlist,
      ctaVariant: "secondary",
      badge: "Coming Soon",
    },
  ];

  return (
    <section id="deployment-options" className="landing-deployment">
      <div className="landing-section-inner">
        <p className="landing-section-label">Deploy Your Way</p>
        <h2 className="landing-section-title">Two Ways to Run</h2>
        <p className="landing-section-subtitle">
          Install locally in under 2 minutes, or let us handle the infrastructure.
        </p>

        <div className="landing-deployment-grid">
          {options.map((option, index) => (
            <div
              key={index}
              className={`landing-deployment-card ${option.highlighted ? "highlighted" : ""}`}
            >
              {option.badge && (
                <div className="landing-deployment-badge">{option.badge}</div>
              )}

              <div className="landing-deployment-header">
                <h3 className="landing-deployment-name">{option.name}</h3>
                <p className="landing-deployment-promise">{option.promise}</p>
              </div>

              <ul className="landing-deployment-features">
                {option.features.map((feature, featureIndex) => (
                  <li key={featureIndex}>
                    <CheckCircleIcon width={18} height={18} className="landing-deployment-check" />
                    {feature}
                  </li>
                ))}
              </ul>

              <Button
                variant={option.ctaVariant}
                size="lg"
                className="landing-deployment-cta"
                onClick={option.ctaAction}
              >
                {option.ctaText}
              </Button>
            </div>
          ))}
        </div>
      </div>

      {showWaitlist && <WaitlistModal onClose={() => setShowWaitlist(false)} source="deployment_hosted" />}
    </section>
  );
}
