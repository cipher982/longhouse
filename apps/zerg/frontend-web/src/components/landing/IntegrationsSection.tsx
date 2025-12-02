export function IntegrationsSection() {
  const integrations = [
    { name: "Slack", icon: "💬", category: "Notifications" },
    { name: "Discord", icon: "🎮", category: "Notifications" },
    { name: "Email", icon: "📧", category: "Notifications" },
    { name: "SMS", icon: "📱", category: "Notifications" },
    { name: "GitHub", icon: "🐙", category: "Development" },
    { name: "Jira", icon: "📋", category: "Development" },
    { name: "Linear", icon: "⚡", category: "Development" },
    { name: "Notion", icon: "📝", category: "Productivity" },
    { name: "Google Calendar", icon: "📅", category: "Productivity" },
    { name: "Apple Health", icon: "❤️", category: "Health" },
    { name: "Home Assistant", icon: "🏠", category: "Smart Home" },
    { name: "Any MCP Server", icon: "🔌", category: "Custom" }
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

