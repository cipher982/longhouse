/**
 * WaitlistModal
 *
 * Shared modal for collecting hosted waitlist signups.
 * Used by DeploymentOptions and PricingSection.
 */

import { useState } from "react";
import { Button } from "../ui";
import config from "../../lib/config";

interface WaitlistModalProps {
  onClose: () => void;
  source?: string;
}

export function WaitlistModal({ onClose, source = "waitlist" }: WaitlistModalProps) {
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
        body: JSON.stringify({ email, source }),
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
