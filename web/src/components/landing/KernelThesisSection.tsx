interface ThesisCard {
  title: string;
  description: string;
}

const cards: ThesisCard[] = [
  {
    title: "A session stays addressable",
    description:
      "It is not just history. You can inspect it, message it, tail it, and continue it later.",
  },
  {
    title: "One session, more capability",
    description:
      "Longhouse in the launch path changes what you can do with a session later. It does not create a second class of session.",
  },
  {
    title: "Laptop now. Durable machine later.",
    description:
      "You can start on your laptop, then move to a box that stays on when you want stronger continuity.",
  },
];

export function KernelThesisSection() {
  return (
    <section className="landing-kernel-thesis">
      <div className="landing-section-inner">
        <p className="landing-section-label">Why It Works</p>
        <h2 className="landing-section-title">Built around real sessions.</h2>
        <p className="landing-section-subtitle">
          Longhouse keeps the session, the machine context, and the control surface tied together so you
          can come back later and keep working.
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
