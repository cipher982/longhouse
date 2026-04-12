interface ThesisCard {
  title: string;
  description: string;
}

const cards: ThesisCard[] = [
  {
    title: "Search across every session",
    description:
      "Find the session where you already solved auth, retries, or that migration. No more grepping JSONL logs by hand.",
  },
  {
    title: "Pick up where you left off",
    description:
      "Open the raw transcript and tool history. Continue from the exact context instead of starting over.",
  },
  {
    title: "Steer sessions after launch",
    description:
      "Start through Longhouse and the session stays reachable. Message it, tail it, or continue it later from browser, CLI, or API.",
  },
];

export function KernelThesisSection() {
  return (
    <section className="landing-kernel-thesis">
      <div className="landing-section-inner">
        <p className="landing-section-label">Why Longhouse</p>
        <h2 className="landing-section-title">Your sessions are worth more than a transcript.</h2>
        <p className="landing-section-subtitle">
          Every session you run is prior art. Longhouse makes it searchable, inspectable, and controllable.
        </p>

        <div className="landing-thesis-grid">
          {cards.map((card, index) => (
            <article key={card.title} className="landing-thesis-card">
              <span className="landing-thesis-number" aria-hidden="true">
                {index + 1}
              </span>
              <h3>{card.title}</h3>
              <p>{card.description}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
