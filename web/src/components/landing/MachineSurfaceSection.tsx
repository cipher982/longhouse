type SurfaceCard = {
  title: string;
  description: string;
  code: string;
};

const cards: SurfaceCard[] = [
  {
    title: "See what's running",
    description: "List active sessions across all your machines and repos.",
    code: "longhouse wall --json",
  },
  {
    title: "Message a live session",
    description: "Send instructions to a running session from terminal or API.",
    code: `longhouse message SESSION_ID "Check the failing test"`,
  },
];

export function MachineSurfaceSection() {
  return (
    <section className="landing-machine-surface" id="surface">
      <div className="landing-section-inner">
        <p className="landing-section-label">CLI + API</p>
        <h2 className="landing-section-title">Use it from terminal, not just the browser.</h2>
        <p className="landing-section-subtitle">
          Browser, CLI, and <code>/api/agents/*</code> share the same session surface. You do not need an
          MCP host or a web UI just to steer live work.
        </p>

        <div className="landing-surface-grid">
          {cards.map((card) => (
            <article key={card.title} className="landing-surface-card">
              <h3>{card.title}</h3>
              <p>{card.description}</p>
              <pre
                className="landing-code-block"
                tabIndex={0}
                aria-label={`${card.title} command example`}
              >
                <code>{card.code}</code>
              </pre>
            </article>
          ))}
        </div>

        <p className="landing-surface-note">
          Browser, CLI, and HTTP API all share the same session model. <a href="https://github.com/cipher982/longhouse" target="_blank" rel="noopener noreferrer">See the full API reference &rarr;</a>
        </p>
      </div>
    </section>
  );
}
