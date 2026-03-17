import { Link } from "react-router-dom";
import {
  CodeIcon,
  SparklesIcon,
  LockIcon,
  ShieldIcon,
  TrashIcon,
  BanIcon,
} from "../icons";
import { getLaunchProviderSupportList } from "../../lib/providers";

interface Provider {
  name: string;
  icon: React.ReactNode;
  status: "live" | "coming";
  description: string;
  statusLabel: string;
}

export function IntegrationsSection() {
  const providerIcons: Record<string, React.ReactNode> = {
    claude: <SparklesIcon width={40} height={40} />,
    codex: <CodeIcon width={40} height={40} />,
    gemini: <SparklesIcon width={40} height={40} />,
  };

  const providers: Provider[] = [
    ...getLaunchProviderSupportList().map((provider) => ({
      name: provider.marketingName,
      icon: providerIcons[provider.id],
      status: "live" as const,
      description: provider.cardDescription,
      statusLabel: provider.statusLabel,
    })),
    {
      name: "OpenCode",
      icon: <CodeIcon width={40} height={40} />,
      status: "coming",
      description: "Open-source AI terminal agent",
      statusLabel: "Coming soon",
    },
    {
      name: "Cursor",
      icon: <CodeIcon width={40} height={40} />,
      status: "coming",
      description: "IDE-integrated AI sessions",
      statusLabel: "Coming soon",
    },
  ];

  return (
    <section id="integrations" className="landing-integrations">
      <div className="landing-section-inner">
        <h2 className="landing-section-title">Session Sources</h2>
        <p className="landing-section-subtitle">
          One timeline for all your AI coding agents.
        </p>

        <div className="landing-providers-grid">
          {providers.map((provider, index) => (
            <div
              key={index}
              className={`landing-provider-card ${provider.status === "coming" ? "coming-soon" : ""}`}
              style={{ animationDelay: `${index * 100}ms` }}
            >
              <span className="landing-provider-icon">{provider.icon}</span>
              <div className="landing-provider-info">
                <span className="landing-provider-name">{provider.name}</span>
                <span className="landing-provider-desc">{provider.description}</span>
              </div>
              <span className={`landing-provider-status ${provider.status}`}>
                {provider.statusLabel}
              </span>
            </div>
          ))}
        </div>

        <p className="landing-providers-tagline">
          Find where you solved auth. Resume that refactor. Start cloud work from the same timeline.
        </p>

        <p className="landing-providers-tagline landing-providers-tagline--subtle">
          Claude currently has the richest hooks and telemetry. Codex and Gemini already sync into the timeline and can start cloud sessions, but direct web continuation is still Claude-first.
        </p>

        {/* Trust badges */}
        <Link to="/security" className="landing-trust-badges-link">
          <div className="landing-trust-badges">
            <div className="landing-trust-badge">
              <LockIcon width={18} height={18} className="landing-trust-icon-svg" />
              <span>Credentials encrypted</span>
            </div>
            <div className="landing-trust-badge">
              <ShieldIcon width={18} height={18} className="landing-trust-icon-svg" />
              <span>HTTPS everywhere</span>
            </div>
            <div className="landing-trust-badge">
              <TrashIcon width={18} height={18} className="landing-trust-icon-svg" />
              <span>Full data deletion</span>
            </div>
            <div className="landing-trust-badge">
              <BanIcon width={18} height={18} className="landing-trust-icon-svg" />
              <span>No training on your data</span>
            </div>
          </div>
          <p className="landing-trust-link-text">Learn more about our security practices →</p>
        </Link>
      </div>
    </section>
  );
}
