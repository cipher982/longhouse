type SurfaceCard = {
  title: string;
  description: string;
  code: string;
};

const cards: SurfaceCard[] = [
  {
    title: "Read the wall",
    description: "See active and recent sessions around a repo or project.",
    code: "longhouse wall --json",
  },
  {
    title: "Inspect a session",
    description: "Tail the latest events or fetch the full machine-facing session detail.",
    code: "longhouse tail SESSION_ID\nlonghouse sessions get SESSION_ID --json",
  },
  {
    title: "Coordinate work",
    description: "Send a directed session message and read the durable inbox.",
    code: "longhouse message TARGET_ID \"Inspect the failing test\"\nlonghouse check-messages --json",
  },
  {
    title: "Continue from the API",
    description: "Resume work from terminal or call the session API directly.",
    code: `curl -N \\
  -X POST \\
  -H "X-Agents-Token: $LONGHOUSE_TOKEN" \\
  -H "X-Longhouse-Session-Id: $CURRENT_SESSION_ID" \\
  -H "Content-Type: application/json" \\
  -d '{"message":"Continue from the API route and keep going."}' \\
  "$LONGHOUSE_URL/api/agents/sessions/$SESSION_ID/continue"`,
  },
];

export function MachineSurfaceSection() {
  return (
    <section className="landing-machine-surface" id="surface">
      <div className="landing-section-inner">
        <p className="landing-section-label">CLI + API</p>
        <h2 className="landing-section-title">Use it from terminal, not just the browser.</h2>
        <p className="landing-section-subtitle">
          The same session works from browser, CLI, and <code>/api/agents/*</code>. You do not need an MCP
          host or a web UI just to steer live work.
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

        <div className="landing-surface-note">
          <p>
            One timeline, one session model. When a session starts through Longhouse, browser, CLI, and API
            all share the same control path.
          </p>
          <p>
            The timeline still matters, but it is one surface on top of the same session model.
          </p>
        </div>
      </div>
    </section>
  );
}
