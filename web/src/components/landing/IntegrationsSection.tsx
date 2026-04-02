import {
  CodeIcon,
  SparklesIcon,
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

  const providers: Provider[] = getLaunchProviderSupportList().map((provider) => ({
    name: provider.marketingName,
    icon: providerIcons[provider.id],
    status: "live" as const,
    description: provider.cardDescription,
    statusLabel: provider.statusLabel,
  }));

  return (
    <section id="providers" className="landing-integrations">
      <div className="landing-section-inner">
        <p className="landing-section-label">Provider Truth</p>
        <h2 className="landing-section-title">Be honest about support.</h2>
        <p className="landing-section-subtitle">
          Claude is strongest today. Archive support is broader than continuation parity. That honesty builds trust.
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
          Longhouse should tell the truth plainly: search and coordination are broad today; direct continuation is still Claude-first.
        </p>

        <p className="landing-providers-tagline landing-providers-tagline--subtle">
          Claude currently has the richest hooks and telemetry. Codex and Gemini already sync into the timeline and can start cloud sessions, but direct web continuation is still Claude-first.
        </p>
      </div>
    </section>
  );
}
