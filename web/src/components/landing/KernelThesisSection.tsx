interface ValueProp {
  action: string;
  title: string;
  description: string;
}

const props: ValueProp[] = [
  {
    action: "Import once",
    title: "Every session lands in one timeline",
    description:
      "Bring Claude, Codex, Antigravity, and OpenCode sessions into a searchable archive immediately. Legacy Gemini imports remain searchable.",
  },
  {
    action: "Launch through Longhouse",
    title: "Keep a control path attached",
    description:
      "Start sessions through Longhouse when you want to message, tail, or continue them later from browser, CLI, or API.",
  },
  {
    action: "Move when ready",
    title: "Laptop to try, always-on box to keep",
    description:
      "Run everything locally to prove it. Move the Runtime Host to a VPS, Mac mini, or homelab box when you want it to stay on.",
  },
];

export function KernelThesisSection() {
  return (
    <section id="how-it-works" className="landing-value-props">
      <div className="landing-section-inner">
        <p className="landing-section-label">How It Works</p>
        <h2 className="landing-section-title">
          One session model. Three moves.
        </h2>

        <div className="landing-value-grid">
          {props.map((prop) => (
            <article key={prop.title} className="landing-value-card">
              <span className="landing-value-action">{prop.action}</span>
              <h3>{prop.title}</h3>
              <p>{prop.description}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
