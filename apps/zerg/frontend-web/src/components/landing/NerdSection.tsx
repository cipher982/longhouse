export function NerdSection() {
  const features = [
    {
      icon: "🔧",
      title: "Custom agents & workflows",
      description: "Build without fighting YAML configs"
    },
    {
      icon: "🔌",
      title: "Connect anything",
      description: "Webhooks, APIs, and MCP servers"
    },
    {
      icon: "🧠",
      title: "Bring your own LLM keys",
      description: "Tune cost/latency to your needs"
    },
    {
      icon: "📊",
      title: "Step-by-step logs",
      description: "Inspect exactly what your agents did"
    },
    {
      icon: "⚡",
      title: "Scheduled or triggered",
      description: "Run on schedule or event-driven"
    },
    {
      icon: "🎨",
      title: "Visual canvas",
      description: "Drag-and-drop workflow builder"
    }
  ];

  const techHighlights = [
    "LangGraph-powered agent execution",
    "Per-token LLM streaming over WebSocket",
    "MCP (Model Context Protocol) integration",
    "Two-tier credential management"
  ];

  return (
    <section className="landing-nerd">
      <div className="landing-section-inner">
        <div className="landing-nerd-header">
          <span className="landing-nerd-badge">For builders</span>
          <h2 className="landing-section-title">For People Who Like Knobs</h2>
          <p className="landing-section-subtitle">
            Power users and hackers, this one's for you.
          </p>
        </div>

        <div className="landing-nerd-grid">
          {features.map((feature, index) => (
            <div key={index} className="landing-nerd-feature">
              <span className="landing-nerd-icon">{feature.icon}</span>
              <h3>{feature.title}</h3>
              <p>{feature.description}</p>
            </div>
          ))}
        </div>

        <div className="landing-nerd-tech">
          <h3>Under the hood</h3>
          <div className="landing-nerd-tech-list">
            {techHighlights.map((tech, index) => (
              <span key={index} className="landing-nerd-tech-item">
                <code>{tech}</code>
              </span>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

