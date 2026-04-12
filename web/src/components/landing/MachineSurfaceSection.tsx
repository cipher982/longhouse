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
    title: "Read the live tail",
    description: "Watch the recent event stream for a session from any machine.",
    code: "longhouse tail SESSION_ID",
  },
  {
    title: "Continue from recovered context",
    description: "Re-open the exact session context that matters instead of starting over.",
    code: "longhouse continue SESSION_ID",
  },
];

export function MachineSurfaceSection() {
  return (
    <section className="landing-machine-surface" id="surface">
      <div className="landing-section-inner">
        <p className="landing-section-label">CLI + API</p>
        <h2 className="landing-section-title">The machine surface is real.</h2>
        <p className="landing-section-subtitle">
          Wall, tail, continue, and <code>/api/agents/*</code> are first-class. The browser is the bundled
          workspace on top of the same session model, not a separate source of truth.
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
          Browser, CLI, and HTTP all share the same session model. Read the{" "}
          <a href="/docs/cli">CLI reference</a> or the <a href="/docs/api">machine API docs</a> for the full surface.
        </p>
      </div>
    </section>
  );
}
