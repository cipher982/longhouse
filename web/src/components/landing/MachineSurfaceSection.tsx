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
    code: "longhouse message TARGET_ID \"Inspect the failing test\"\nlonghouse messages --json",
  },
  {
    title: "Control live sessions",
    description: "Send live messages to running sessions from terminal or API.",
    code: `curl -X POST \\
  -H "X-Agents-Token: $LONGHOUSE_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"message":"Check that test"}' \\
  "$LONGHOUSE_URL/api/agents/sessions/$SESSION_ID/send-live"`,
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

        <div className="landing-surface-note">
          <p>
            One timeline, one session model. When a session starts through Longhouse, browser, CLI, and API
            all speak to the same session surface.
          </p>
          <p>
            The timeline still matters, but it is one surface on top of the same session model.
          </p>
        </div>
      </div>
    </section>
  );
}
