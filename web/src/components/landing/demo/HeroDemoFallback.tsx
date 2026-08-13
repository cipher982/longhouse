const TERMINALS = [
  {
    title: "Claude Code",
    detail: "macbook",
    accent: "#E8875A",
    lines: ["Fix the inventory count bug", "Reading inventory.py", "Editing count_items"],
  },
  {
    title: "Codex",
    detail: "devbox",
    accent: "#7BC9A8",
    lines: ["Repair the release build", "Inspecting the failing target", "Running focused tests"],
  },
  {
    title: "OpenCode",
    detail: "homelab",
    accent: "#6FB7E8",
    lines: ["Run the nightly digest", "$ python3 test_inventory.py", "all tests passed"],
  },
] as const;

/**
 * Lightweight first paint for the lazy recorded-terminal bundle.
 *
 * Keep this as plain DOM so a cold visit has a complete hero before the
 * recording data arrives. Its geometry mirrors AgentsBeat closely enough that
 * Suspense can replace it without flashing an empty media frame or shifting
 * the page.
 */
export function HeroDemoFallback() {
  return (
    <div className="hero-demo hero-demo-fallback" aria-hidden="true">
      <div className="hero-demo-stage">
        <div className="hero-demo-beat">
          <div className="hero-demo-agents">
            {TERMINALS.map((terminal) => (
              <div className="hero-demo-agents-tile" key={terminal.title}>
                <div className="hero-demo-terminal">
                  <div className="hero-demo-terminal-chrome">
                    <span className="hero-demo-terminal-dots">
                      <i /><i /><i />
                    </span>
                    <span
                      className="hero-demo-terminal-title"
                      style={{ color: terminal.accent }}
                    >
                      {terminal.title}
                    </span>
                    <span className="hero-demo-terminal-detail">{terminal.detail}</span>
                  </div>
                  <div className="hero-demo-terminal-body">
                    <div className="hero-demo-terminal-screen hero-demo-fallback-screen">
                      {terminal.lines.map((line, index) => (
                        <span
                          className={index === terminal.lines.length - 1 ? "is-active" : undefined}
                          key={line}
                        >
                          {line}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
      <div className="hero-demo-footer">
        <p className="hero-demo-caption">Your coding agents already run everywhere.</p>
        <div className="hero-demo-dots">
          <span className="hero-demo-dot is-active" />
          <span className="hero-demo-dot" />
          <span className="hero-demo-dot" />
          <span className="hero-demo-dot" />
        </div>
      </div>
    </div>
  );
}
