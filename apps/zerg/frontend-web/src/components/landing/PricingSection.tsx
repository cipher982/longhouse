/**
 * PricingSection
 *
 * Simple 2-tier pricing: Free Beta + Pro (coming soon)
 */

import { useState } from "react";
import config from "../../lib/config";
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
        body: JSON.stringify({ email, source: "pricing_pro" }),
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
          ×
        </button>

        {result ? (
          <div className="waitlist-result">
            <div className={`waitlist-result-icon ${result.success ? "success" : "error"}`}>
              {result.success ? "✓" : "!"}
            </div>
            <p className="waitlist-result-message">{result.message}</p>
            <Button variant="primary" size="lg" onClick={onClose}>
              Got it
            </Button>
          </div>
        ) : (
          <>
            <h2>Join the Pro Waitlist</h2>
            <p className="landing-login-subtext">
              Be the first to know when Swarmlet Pro launches with unlimited fiches and advanced
              workflows.
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

export function PricingSection() {
  const [showWaitlist, setShowWaitlist] = useState(false);

  const handleGetStarted = () => {
    // If auth is disabled (dev mode), go directly to dashboard
    if (!config.authEnabled) {
      window.location.href = "/dashboard";
      return;
    }
    // Scroll to top and trigger login
    window.scrollTo({ top: 0, behavior: "smooth" });
    setTimeout(() => {
      document.querySelector<HTMLButtonElement>(".landing-cta-main")?.click();
    }, 500);
  };

  const handleJoinWaitlist = () => {
    setShowWaitlist(true);
  };

  const tiers: PricingTier[] = [
    {
      name: "Free Beta",
      price: "$0",
      period: "/month",
      description: "Everything you need to get started",
      features: ["5 AI fiches", "Core integrations", "Community support", "Basic automations"],
      ctaText: "Start Free Beta",
      ctaAction: handleGetStarted,
      highlighted: true,
    },
    {
      name: "Pro",
      price: "$9",
      period: "/month",
      description: "For power users",
      features: [
        "Unlimited fiches",
        "All integrations",
        "Priority support",
        "Advanced workflows",
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
        <p className="landing-section-subtitle">Start free. Upgrade when you need more.</p>

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

      {showWaitlist && <WaitlistModal onClose={() => setShowWaitlist(false)} />}
    </section>
  );
}
