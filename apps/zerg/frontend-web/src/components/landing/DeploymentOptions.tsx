/**
 * DeploymentOptions
 *
 * Two-card section showing deployment paths:
 * Self-hosted (primary) and Hosted Beta.
 */

import { useState } from "react";
import { Button } from "../ui";
import { CheckCircleIcon } from "../icons";
import config from "../../lib/config";

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

interface WaitlistModalProps {
  onClose: () => void;
}

function WaitlistModal({ onClose }: WaitlistModalProps) {
  const [email, setEmail] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [result, setResult] = useState<{ success: boolean; message: string } | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email || isSubmitting) return;

    setIsSubmitting(true);
    try {
      const response = await fetch(`${config.apiBaseUrl}/waitlist`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, source: "deployment_hosted" }),
      });
      const data = await response.json();
      setResult({ success: data.success, message: data.message });
    } catch {
      setResult({ success: false, message: "Something went wrong. Please try again." });
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="landing-login-overlay" onClick={onClose}>
      <div className="landing-login-modal waitlist-modal" onClick={(e) => e.stopPropagation()}>
        <button className="landing-login-close" onClick={onClose}>
          x
        </button>

        {result ? (
          <div className="waitlist-result">
            <div className={`waitlist-result-icon ${result.success ? "success" : "error"}`}>
              {result.success ? "OK" : "!"}
            </div>
            <p className="waitlist-result-message">{result.message}</p>
            <Button variant="primary" size="lg" onClick={onClose}>
              Got it
            </Button>
          </div>
        ) : (
          <>
            <h2>Join the Hosted Waitlist</h2>
            <p className="landing-login-subtext">
              Be the first to know when Longhouse hosted launches with always-on sync and cross-device access.
            </p>

            <form onSubmit={handleSubmit} className="waitlist-form">
              <input
                type="email"
                placeholder="Enter your email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="waitlist-input"
                required
                autoFocus
              />
              <Button type="submit" variant="primary" size="lg" disabled={isSubmitting}>
                {isSubmitting ? "Joining..." : "Join Waitlist"}
              </Button>
            </form>

            <p className="waitlist-privacy">No spam, ever. Unsubscribe anytime.</p>
          </>
        )}
      </div>
    </div>
  );
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
      promise: "Full control",
      features: [
        "Your data stays local",
        "No account needed",
        "Works offline",
        "Open source (Apache 2.0)",
      ],
      ctaText: "Install CLI",
      ctaAction: handleInstallCLI,
      ctaVariant: "primary",
      highlighted: true,
    },
    {
      name: "Hosted Beta",
      promise: "Zero setup",
      features: [
        "Managed hosting",
        "Automatic updates",
        "Team sharing",
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
        <h2 className="landing-section-title">Choose Your Path</h2>
        <p className="landing-section-subtitle">
          Run locally with full control, or let us handle hosting.
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

      {showWaitlist && <WaitlistModal onClose={() => setShowWaitlist(false)} />}
    </section>
  );
}
