interface ThesisCard {
  title: string;
  description: string;
}

const cards: ThesisCard[] = [
  {
    title: "The session is the durable object",
    description:
      "A session is not a dead transcript. It is something you can inspect, address, tail, and continue.",
  },
  {
    title: "The machine surface is real",
    description:
      "Longhouse works from terminal and HTTP first. The web UI is the bundled human view on top of the same kernel.",
  },
  {
    title: "Works on your laptop. Shines on a machine that stays on.",
    description:
      "Self-hosted is the free default path. A durable machine makes the product better, but laptop users can still get value immediately.",
  },
];

export function KernelThesisSection() {
  return (
    <section className="landing-kernel-thesis">
      <div className="landing-section-inner">
        <p className="landing-section-label">Kernel Thesis</p>
        <h2 className="landing-section-title">Not another AI dashboard.</h2>
        <p className="landing-section-subtitle">
          Lead with the outcome: control sessions after launch. Then explain why it works: the session is
          the durable object, and the machine surface is real.
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
