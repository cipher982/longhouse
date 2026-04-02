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
    title: "Continue from the kernel",
    description: "Resume work from terminal or call the machine surface directly.",
    code: `curl -N \\
  -H "X-Agents-Token: $LONGHOUSE_TOKEN" \\
  "$LONGHOUSE_URL/api/agents/sessions/$SESSION_ID/continue"`,
  },
];

export function MachineSurfaceSection() {
  return (
    <section className="landing-machine-surface" id="surface">
      <div className="landing-section-inner">
        <p className="landing-section-label">Machine Surface</p>
        <h2 className="landing-section-title">Show the terminal seam early.</h2>
        <p className="landing-section-subtitle">
          The product should not read like “just a website.” The same kernel is reachable from CLI and
          <code> /api/agents/*</code>.
        </p>

        <div className="landing-surface-grid">
          {cards.map((card) => (
            <article key={card.title} className="landing-surface-card">
              <h3>{card.title}</h3>
              <p>{card.description}</p>
              <pre className="landing-code-block">
                <code>{card.code}</code>
              </pre>
            </article>
          ))}
        </div>

        <div className="landing-surface-note">
          <p>
            <strong>Canonical product line:</strong> Find the session. Ask it. Continue it.
          </p>
          <p>
            The timeline still matters, but it stops pretending to be the entire product boundary.
          </p>
        </div>
      </div>
    </section>
  );
}
