interface Step {
  number: string;
  title: string;
  description: string;
}

const steps: Step[] = [
  {
    number: "1",
    title: "Bring sessions into one timeline",
    description:
      "Bring in real Claude, Codex, or Gemini sessions before changing your workflow. Demo data is only the safe preview path.",
  },
  {
    number: "2",
    title: "Find the exact prior solution",
    description:
      "Search for the session where auth, retries, or a refactor was already solved instead of grepping provider logs by hand.",
  },
  {
    number: "3",
    title: "Open the raw transcript",
    description:
      "Open the exact transcript and tool history that matters. The model already did the work; Longhouse makes it reusable.",
  },
  {
    number: "4",
    title: "Start through Longhouse for live control",
    description:
      "When Longhouse is in the launch path, the session stays reachable later from browser, CLI, or API through an explicit control capability.",
  },
  {
    number: "5",
    title: "Message it or continue it later",
    description:
      "Use the wall, send the session a message, or continue it from the recovered context when you come back.",
  },
];

export function HowItWorksSection() {
  return (
    <section id="journey" className="landing-how-it-works landing-proof-journey">
      <div className="landing-section-inner">
        <p className="landing-section-label">How It Clicks</p>
        <h2 className="landing-section-title">One timeline. Then keep control.</h2>
        <p className="landing-section-subtitle">
          First you recover something you already solved. Then you see that the same timeline can still
          steer live work later when Longhouse kept the control path open.
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
            <p className="landing-journey-note-label">What changes</p>
            <blockquote>Find the session. Ask it. Continue it.</blockquote>
            <p>
              The session stops being a dead transcript. It becomes something you can search, return to,
              and steer from more than one surface. Longhouse changes capability, not what the session is.
            </p>
          </aside>
        </div>
      </div>
    </section>
  );
}
