interface Step {
  number: string;
  title: string;
  description: string;
}

const steps: Step[] = [
  {
    number: "1",
    title: "See sessions immediately",
    description:
      "Install Longhouse locally and land on demo data or real shipped sessions before billing or account friction.",
  },
  {
    number: "2",
    title: "Find the prior solution",
    description:
      "Search for the session where auth, retries, or a refactor was already solved instead of grepping provider logs by hand.",
  },
  {
    number: "3",
    title: "Inspect the raw detail",
    description:
      "Open the exact transcript and tool history that matters. The model already did the work; Longhouse makes it reusable.",
  },
  {
    number: "4",
    title: "Coordinate through the kernel",
    description:
      "Show the wall, tail the session, or send a directed message. The coordination surface matters as much as the search surface.",
  },
  {
    number: "5",
    title: "Continue and keep going",
    description:
      "Resume the current session from the recovered context. Optional final beat: show the same session reachable from another device.",
  },
];

export function HowItWorksSection() {
  return (
    <section id="journey" className="landing-how-it-works landing-proof-journey">
      <div className="landing-section-inner">
        <p className="landing-section-label">Proof Of Value</p>
        <h2 className="landing-section-title">The demo should feel like recovered leverage.</h2>
        <p className="landing-section-subtitle">
          The strongest story is not “look at this website.” It is “I found prior work, recovered context,
          coordinated through the kernel, and kept going.”
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
            <p className="landing-journey-note-label">Canonical line</p>
            <blockquote>Find the session. Ask it. Continue it.</blockquote>
            <p>
              That line is short enough for the hero, concrete enough for the product, and specific enough
              to differentiate from generic AI workspace tools.
            </p>
          </aside>
        </div>
      </div>
    </section>
  );
}
