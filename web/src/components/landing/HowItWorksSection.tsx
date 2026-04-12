interface Step {
  number: string;
  title: string;
  description: string;
}

const steps: Step[] = [
  {
    number: "1",
    title: "Install on the machine where you already work",
    description:
      "Start locally on macOS, Linux, or WSL. The first proof is seeing one real session show up fast.",
  },
  {
    number: "2",
    title: "Choose where durability should live",
    description:
      "Run both pieces on your laptop for a tryout, or point your machines at an always-on Runtime Host when you want Longhouse available after the lid closes.",
  },
  {
    number: "3",
    title: "Bring existing sessions in immediately",
    description:
      "Claude, Codex, and Gemini sessions can land in the archive without forcing a brand-new workflow first.",
  },
  {
    number: "4",
    title: "Start through Longhouse when the session should stay reachable",
    description:
      "Use explicit Longhouse launch commands when you want control after launch instead of a dead transcript later.",
  },
  {
    number: "5",
    title: "Find it, inspect it, and steer it later",
    description:
      "Use the timeline, wall, tail, directed messages, or continue from recovered context when you come back.",
  },
];

export function HowItWorksSection() {
  return (
    <section id="journey" className="landing-how-it-works landing-proof-journey">
      <div className="landing-section-inner">
        <p className="landing-section-label">How It Works</p>
        <h2 className="landing-section-title">Install locally. Put durability where it belongs.</h2>
        <p className="landing-section-subtitle">
          Work happens on your machine. Longhouse keeps the archive and control path attached without pretending
          sessions moved somewhere magical.
        </p>

        <div className="landing-journey-grid">
          <div className="landing-journey-list">
            {steps.map((step) => (
              <article key={step.title} className="landing-journey-step">
                <div className="landing-step-number">{step.number}</div>
                <div className="landing-journey-step-body">
                  <h3 className="landing-step-title">{step.title}</h3>
                  <p className="landing-step-description">{step.description}</p>
                </div>
              </article>
            ))}
          </div>

          <aside className="landing-journey-note">
            <p className="landing-journey-note-label">The honest model</p>
            <blockquote>One session. Explicit capability states.</blockquote>
            <p>
              Imported sessions are search-first. Longhouse-launched sessions can also keep live control or
              a host-reattach path. A sleeping laptop is not a system failure. It just means durability
              belongs on a machine that stays on.
            </p>
          </aside>
        </div>
      </div>
    </section>
  );
}
