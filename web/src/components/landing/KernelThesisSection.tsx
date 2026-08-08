export function KernelThesisSection() {
  return (
    <section id="control" className="landing-thesis">
      <div className="landing-section-inner landing-thesis-inner">
        <div className="landing-thesis-copy">
          <h2 className="landing-thesis-title">
            Start through Longhouse when you want remote control.
          </h2>
          <p className="landing-thesis-lead">
            The provider still runs on the machine you choose, using its normal account,
            tools, configuration, and repository. Longhouse keeps the connection needed
            to check progress and use the controls that provider supports.
          </p>

          <ul className="landing-thesis-points">
            <li>
              <strong>Sessions started outside Longhouse</strong>
              <span>appear in the timeline with their transcripts and tool calls.</span>
            </li>
            <li>
              <strong>Sessions started through Longhouse</strong>
              <span>can add send, interrupt, mid-turn steering, or resume controls.</span>
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
