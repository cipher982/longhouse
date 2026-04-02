interface Step {
  number: string;
  title: string;
  description: string;
}

const steps: Step[] = [
  {
    number: "1",
    title: "Bring in work you already did",
    description:
      "Install Longhouse and import real Claude, Codex, or Gemini sessions before changing your workflow. Demo data is only the fallback preview path.",
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
    title: "Start a Longhouse session when you want control",
    description:
      "New Longhouse sessions are the second beat: they are the ones you can steer after launch from more than one surface.",
  },
  {
    number: "5",
    title: "Coordinate and continue later",
    description:
      "Show the wall, message the session, or continue it from the recovered context. That is the proof this is more than a dashboard.",
  },
];

export function HowItWorksSection() {
  return (
    <section id="journey" className="landing-how-it-works landing-proof-journey">
      <div className="landing-section-inner">
        <p className="landing-section-label">Proof Of Value</p>
        <h2 className="landing-section-title">The demo should feel like two beats, not one.</h2>
        <p className="landing-section-subtitle">
          First the user finds something they already did. Then they see that a real session can still be
          steered after launch.
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
              Keep this as the mechanic line. The emotional hook is control after launch; this line proves
              what that control feels like.
            </p>
          </aside>
        </div>
      </div>
    </section>
  );
}
