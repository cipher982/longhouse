/**
 * HowItWorksSection
 *
 * Simple 3-step explanation of how Longhouse works.
 * Cloud workspace: Sync → Run → Resume.
 */

import config from "../../lib/config";
import { ZapIcon, SmartphoneIcon, PlugIcon } from "../icons";
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
    title: "Sync Sessions",
    description: "Install CLI. Your Claude Code sessions flow to the cloud."
  },
  {
    icon: <ZapIcon width={32} height={32} />,
    number: "2",
    title: "Agents Run",
    description: "Work happens on our servers. Close your laptop—nothing stops."
  },
  {
    icon: <SmartphoneIcon width={32} height={32} />,
    number: "3",
    title: "Continue Anywhere",
    description: "Phone, tablet, browser. Pick up where you left off."
  }
];

export function HowItWorksSection() {
  const handleGetStarted = () => {
    if (config.marketingOnly) {
      document.querySelector(".install-section")?.scrollIntoView({ behavior: "smooth" });
      return;
    }
    // If auth is disabled (dev mode), go directly to timeline
    if (!config.authEnabled) {
      window.location.href = '/timeline';
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
          Your cloud workspace. Setup in 2 minutes.
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
            Get your Longhouse 🪵
          </Button>
        </div>
      </div>
    </section>
  );
}
