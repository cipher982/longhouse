export function KernelThesisSection() {
  return (
    <section id="control" className="landing-thesis">
      <div className="landing-section-inner landing-thesis-inner">
        <div className="landing-thesis-copy">
          <h2 className="landing-thesis-title">
            Full control over sessions you launch.
          </h2>
          <p className="landing-thesis-lead">
            The provider CLI keeps running on your machine with its own account, tools,
            and repository. Launch a session through Longhouse and it stays steerable
            from the browser and the iPhone app: type the next instruction, cut off a
            bad turn, or pick up one that died.
          </p>

          <ul className="landing-thesis-points">
            <li>
              <strong>Launch through Longhouse</strong>
              <span>
                and the control surface is live: send, interrupt, resume, and mid-turn
                steering where the provider supports it, while the terminal stays
                usable at your desk.
              </span>
            </li>
            <li>
              <strong>Agents you started elsewhere</strong>
              <span>
                stream in automatically. Watch them and inspect every tool call in the
                same place as your managed sessions; control stays off until you launch
                through Longhouse.
              </span>
            </li>
          </ul>
        </div>

        <div className="landing-thesis-visual">
          <img
            src="/images/landing/phone-session.png"
            alt="A Claude Code session open in Longhouse on an iPhone"
            className="landing-thesis-phone"
            width={1206}
            height={2622}
            loading="lazy"
            decoding="async"
          />
        </div>
      </div>
    </section>
  );
}
