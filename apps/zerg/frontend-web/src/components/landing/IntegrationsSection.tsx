import {
  SlackIcon,
  GamepadIcon,
  MailIcon,
  MessageSquareIcon,
  GithubIcon,
  ClipboardListIcon,
  ZapIcon,
  FileTextIcon,
  CalendarIcon,
  HeartIcon,
  HomeIcon,
  PlugIcon,
} from "../icons";

interface Integration {
  name: string;
  icon: React.ReactNode;
  category: string;
  available: boolean; // true = implemented, false = coming soon
}

export function IntegrationsSection() {
  const integrations: Integration[] = [
    // Available now - these are implemented in backend/zerg/connectors/registry.py
    {
      name: "Slack",
      icon: <SlackIcon width={32} height={32} />,
      category: "Notifications",
      available: true,
    },
    {
      name: "Discord",
      icon: <GamepadIcon width={32} height={32} />,
      category: "Notifications",
      available: true,
    },
    {
      name: "Email",
      icon: <MailIcon width={32} height={32} />,
      category: "Notifications",
      available: true,
    },
    {
      name: "SMS",
      icon: <MessageSquareIcon width={32} height={32} />,
      category: "Notifications",
      available: true,
    },
    {
      name: "GitHub",
      icon: <GithubIcon width={32} height={32} />,
      category: "Development",
      available: true,
    },
    {
      name: "Jira",
      icon: <ClipboardListIcon width={32} height={32} />,
      category: "Development",
      available: true,
    },
    {
      name: "Linear",
      icon: <ZapIcon width={32} height={32} />,
      category: "Development",
      available: true,
    },
    {
      name: "Notion",
      icon: <FileTextIcon width={32} height={32} />,
      category: "Productivity",
      available: true,
    },
    // Coming soon - not yet implemented
    {
      name: "Google Calendar",
      icon: <CalendarIcon width={32} height={32} />,
      category: "Productivity",
      available: false,
    },
    {
      name: "Apple Health",
      icon: <HeartIcon width={32} height={32} />,
      category: "Health",
      available: false,
    },
    {
      name: "Home Assistant",
      icon: <HomeIcon width={32} height={32} />,
      category: "Smart Home",
      available: false,
    },
    {
      name: "MCP Servers",
      icon: <PlugIcon width={32} height={32} />,
      category: "Custom",
      available: true,
    },
  ];

  return (
    <section id="integrations" className="landing-integrations">
      <div className="landing-section-inner">
        <h2 className="landing-section-title">Works With Your Tools</h2>
        <p className="landing-section-subtitle">
          Connect what you already use. No vendor lock-in.
        </p>

        <div className="landing-integrations-grid">
          {integrations.map((integration, index) => (
            <div
              key={index}
              className={`landing-integration-item ${!integration.available ? 'coming-soon' : ''}`}
              style={{ animationDelay: `${index * 50}ms` }}
            >
              <span className="landing-integration-icon">{integration.icon}</span>
              <span className="landing-integration-name">{integration.name}</span>
              {!integration.available && (
                <span className="landing-integration-badge">Soon</span>
              )}
            </div>
          ))}
        </div>

        <p className="landing-integrations-note">
          And anything else via <code>webhooks</code>, <code>REST APIs</code>, or <code>MCP</code>
        </p>
      </div>
    </section>
  );
}
