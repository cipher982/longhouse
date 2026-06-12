interface ValueProp {
  action: string;
  title: string;
  description: string;
}

const props: ValueProp[] = [
  {
    action: "It just syncs",
    title: "Your sessions show up on their own",
    description:
      "Install Longhouse and every Claude Code, Codex, Antigravity, and OpenCode session flows into one searchable timeline — live. No import step, no setup.",
  },
  {
    action: "Walk away",
    title: "Pick up any session from your phone",
    description:
      "Continue a session you started at your desk from your phone or browser — message it, tail it, steer it. Or kick off a new one remotely and let it run.",
  },
  {
    action: "Keep it running",
    title: "Laptop to try, always-on box to keep",
    description:
      "Run it all on your laptop to start. Move the Runtime Host to a VPS, Mac mini, or homelab box so sessions stay live after you close the lid.",
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
