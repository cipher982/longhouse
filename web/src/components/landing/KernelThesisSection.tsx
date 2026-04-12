interface ThesisCard {
  title: string;
  description: string;
}

const cards: ThesisCard[] = [
  {
    title: "Every session lands in one timeline",
    description:
      "Import what you already ran and recover prior art fast instead of grepping provider logs and JSONL files by hand.",
  },
  {
    title: "Start through Longhouse when you want control later",
    description:
      "The session stays addressable. Message it, tail it, or continue it later from browser, CLI, or API.",
  },
  {
    title: "Laptop for tryout. Always-on box for durability.",
    description:
      "Run everything locally to prove value fast. Move the Runtime Host to a VPS, Mac mini, or homelab box when you want it to stay on.",
  },
];

export function KernelThesisSection() {
  return (
    <section className="landing-kernel-thesis">
      <div className="landing-section-inner">
        <p className="landing-section-label">Why Longhouse</p>
        <h2 className="landing-section-title">One session model. Explicit capabilities.</h2>
        <p className="landing-section-subtitle">
          Imported sessions are immediately searchable. Sessions started through Longhouse keep a control path
          attached. Same session, better options later.
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
