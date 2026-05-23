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
    antigravity: <SparklesIcon width={40} height={40} />,
    opencode: <CodeIcon width={40} height={40} />,
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
        <h2 className="landing-section-title">Claude is strongest today. Antigravity and OpenCode round out the lineup.</h2>
        <p className="landing-section-subtitle">
          Claude, Codex, Antigravity, and OpenCode all land in the same timeline. Capability after launch depends
          on how mature each control path is today, and the page should say that plainly.
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
          Codex launch-through-Longhouse is supported; Antigravity and OpenCode are managed observe-only today.
          Claude is still the strongest continuation path. Existing Gemini sessions stay searchable as legacy imports.
        </p>
      </div>
    </section>
  );
}
