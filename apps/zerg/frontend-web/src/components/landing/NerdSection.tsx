import {
  WrenchIcon,
  PlugIcon,
  BrainIcon,
  BarChartIcon,
  ZapIcon,
  PaletteIcon,
} from "../icons";

export function NerdSection() {
  const features = [
    {
      icon: <WrenchIcon width={32} height={32} />,
      title: "Custom agents & workflows",
      description: "Build without fighting YAML configs",
    },
    {
      icon: <PlugIcon width={32} height={32} />,
      title: "Connect anything",
      description: "Webhooks, APIs, and MCP servers",
    },
    {
      icon: <BrainIcon width={32} height={32} />,
      title: "Powered by GPT-4",
      description: "OpenAI models for reliable AI",
    },
    {
      icon: <BarChartIcon width={32} height={32} />,
      title: "Step-by-step logs",
      description: "Inspect exactly what your agents did",
    },
    {
      icon: <ZapIcon width={32} height={32} />,
      title: "Scheduled or triggered",
      description: "Run on schedule or event-driven",
    },
    {
      icon: <PaletteIcon width={32} height={32} />,
      title: "Visual canvas",
      description: "Drag-and-drop workflow builder",
    },
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

        {/* Visual workflow canvas preview */}
        <div className="landing-nerd-canvas">
          <img
            src="/images/landing/canvas-preview.png"
            alt="Visual workflow canvas showing AI agent nodes connected with triggers and actions"
            className="landing-nerd-canvas-image"
          />
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
