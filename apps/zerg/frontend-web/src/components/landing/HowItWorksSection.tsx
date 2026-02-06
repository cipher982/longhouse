/**
 * HowItWorksSection
 *
 * Simple 3-step explanation of how Longhouse works.
 * Dual-path: Hosted or Self-hosted → Search → Resume.
 */

import config from "../../lib/config";
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
    title: "Install",
    description: "Self-host in under 2 minutes, or sign up for hosted beta. Claude Code syncs today; Codex/Cursor/Gemini in progress."
  },
  {
    icon: <SearchIcon width={32} height={32} />,
    number: "2",
    title: "Search",
    description: "Find where you solved it. FTS5-powered instant search across all sessions (launch requirement)."
  },
  {
    icon: <SmartphoneIcon width={32} height={32} />,
    number: "3",
    title: "Resume",
    description: "Continue any conversation from any device. Hosted keeps agents always-on."
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
          Hosted or self-hosted. Setup in 2 minutes.
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
            Get Started
          </Button>
        </div>
      </div>
    </section>
  );
}
