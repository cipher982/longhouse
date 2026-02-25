/**
 * HowItWorksSection
 *
 * Simple 3-step explanation of how Longhouse works.
 * Dual-path: Hosted or Self-hosted → Search → Resume.
 */

import { DownloadIcon, SearchIcon, SmartphoneIcon } from "../icons";
import { Button } from "../ui";

interface Step {
  icon: React.ReactNode;
  number: string;
  title: string;
  description: string;
}

const steps: Step[] = [
  {
    icon: <DownloadIcon width={32} height={32} />,
    number: "1",
    title: "Connect",
    description: "Sign up and install the daemon. Your Claude Code, Codex, and Gemini sessions appear in one live timeline automatically."
  },
  {
    icon: <SmartphoneIcon width={32} height={32} />,
    number: "2",
    title: "Resume Anywhere",
    description: "Close your laptop. Open any device. Click a session and pick up where you left off — full context, live interaction."
  },
  {
    icon: <SearchIcon width={32} height={32} />,
    number: "3",
    title: "Agents Talk",
    description: "Your agents can ask each other questions across sessions. No copy-paste, no log scraping — direct inter-agent communication."
  }
];

export function HowItWorksSection() {
  const handleGetStarted = () => {
    document.getElementById("pricing")?.scrollIntoView({ behavior: "smooth" });
  };

  return (
    <section id="how-it-works" className="landing-how-it-works">
      <div className="landing-section-inner">
        <h2 className="landing-section-title">How It Works</h2>
        <p className="landing-section-subtitle">
          From local sessions to always-on cloud agents in 2 minutes.
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
            See Pricing
          </Button>
        </div>
      </div>
    </section>
  );
}
