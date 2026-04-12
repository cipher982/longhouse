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
        <p className="landing-section-label">Providers</p>
        <h2 className="landing-section-title">Works with Claude, Codex, and Gemini.</h2>
        <p className="landing-section-subtitle">
          All providers land in the same timeline. Claude has the deepest hooks and live control today.
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
          Codex and Gemini sessions are already searchable and inspectable. Live control support is expanding.
        </p>
      </div>
    </section>
  );
}
