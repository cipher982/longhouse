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

export function IntegrationsSection() {
  const integrations = [
    {
      name: "Slack",
      icon: <SlackIcon width={32} height={32} />,
      category: "Notifications",
    },
    {
      name: "Discord",
      icon: <GamepadIcon width={32} height={32} />,
      category: "Notifications",
    },
    {
      name: "Email",
      icon: <MailIcon width={32} height={32} />,
      category: "Notifications",
    },
    {
      name: "SMS",
      icon: <MessageSquareIcon width={32} height={32} />,
      category: "Notifications",
    },
    {
      name: "GitHub",
      icon: <GithubIcon width={32} height={32} />,
      category: "Development",
    },
    {
      name: "Jira",
      icon: <ClipboardListIcon width={32} height={32} />,
      category: "Development",
    },
    {
      name: "Linear",
      icon: <ZapIcon width={32} height={32} />,
      category: "Development",
    },
    {
      name: "Notion",
      icon: <FileTextIcon width={32} height={32} />,
      category: "Productivity",
    },
    {
      name: "Google Calendar",
      icon: <CalendarIcon width={32} height={32} />,
      category: "Productivity",
    },
    {
      name: "Apple Health",
      icon: <HeartIcon width={32} height={32} />,
      category: "Health",
    },
    {
      name: "Home Assistant",
      icon: <HomeIcon width={32} height={32} />,
      category: "Smart Home",
    },
    {
      name: "Any MCP Server",
      icon: <PlugIcon width={32} height={32} />,
      category: "Custom",
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
              className="landing-integration-item"
              style={{ animationDelay: `${index * 50}ms` }}
            >
              <span className="landing-integration-icon">{integration.icon}</span>
              <span className="landing-integration-name">{integration.name}</span>
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

