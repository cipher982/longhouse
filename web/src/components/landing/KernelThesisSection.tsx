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
    title: "Hosted is the upgrade, not the gate",
    description:
      "The first proof of value should happen free and locally. Hosted is what you buy when you want always-on access.",
  },
];

export function KernelThesisSection() {
  return (
    <section className="landing-kernel-thesis">
      <div className="landing-section-inner">
        <p className="landing-section-label">Kernel Thesis</p>
        <h2 className="landing-section-title">Not another AI dashboard.</h2>
        <p className="landing-section-subtitle">
          Longhouse should feel like the missing operating system for CLI agent work: searchable, addressable,
          and resumable from more than one surface.
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
