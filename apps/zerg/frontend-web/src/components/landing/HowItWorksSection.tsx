/**
 * HowItWorksSection
 *
 * Simple 3-step explanation of how Swarmlet works.
 * Replaces the heavier ScenariosSection.
 */

import config from "../../lib/config";
import { PlugIcon, BrainIcon, ZapIcon } from "../icons";
import { Button } from "../ui";

interface Step {
  icon: React.ReactNode;
  number: string;
  title: string;
  description: string;
}

const steps: Step[] = [
  {
    icon: <PlugIcon width={32} height={32} />,
    number: "1",
    title: "Connect",
    description: "Link your apps in 2 minutes. Health trackers, calendars, inboxes, smart home."
  },
  {
    icon: <BrainIcon width={32} height={32} />,
    number: "2",
    title: "AI Learns",
    description: "Your AI builds context from your data. It learns your patterns and preferences."
  },
  {
    icon: <ZapIcon width={32} height={32} />,
    number: "3",
    title: "Automate",
    description: "Morning briefs, smart triggers, proactive alerts. Your AI works while you sleep."
  }
];

export function HowItWorksSection() {
  const handleGetStarted = () => {
    // If auth is disabled (dev mode), go directly to dashboard
    if (!config.authEnabled) {
      window.location.href = '/dashboard';
      return;
    }
    // Scroll to top and trigger login
    window.scrollTo({ top: 0, behavior: 'smooth' });
    setTimeout(() => {
      document.querySelector<HTMLButtonElement>('.landing-cta-main')?.click();
    }, 500);
  };

  return (
    <section id="how-it-works" className="landing-how-it-works">
      <div className="landing-section-inner">
        <h2 className="landing-section-title">How It Works</h2>
        <p className="landing-section-subtitle">
          Three simple steps. No complexity.
        </p>

        <div className="landing-steps-row">
          {steps.map((step, index) => (
            <div key={index} className="landing-step" style={{ animationDelay: `${index * 100}ms` }}>
              <div className="landing-step-icon">
                {step.icon}
              </div>
              <div className="landing-step-number">{step.number}</div>
              <h3 className="landing-step-title">{step.title}</h3>
              <p className="landing-step-description">{step.description}</p>
            </div>
          ))}
        </div>

        <div className="landing-steps-cta">
          <Button variant="primary" size="lg" onClick={handleGetStarted}>
            Get Started Free
          </Button>
        </div>
      </div>
    </section>
  );
}
