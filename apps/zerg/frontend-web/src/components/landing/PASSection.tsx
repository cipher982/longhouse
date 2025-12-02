import { SmartphoneIcon, BellIcon, WrenchIcon } from "../icons";

export function PASSection() {
  return (
    <section className="landing-pas">
      <div className="landing-section-inner">
        {/* Problem */}
        <div className="landing-pas-problem">
          <h2 className="landing-section-label">The Problem</h2>
          <ul className="landing-pas-bullets">
            <li>
              <span className="landing-pas-icon">
                <SmartphoneIcon width={28} height={28} />
              </span>
              <span>
                <strong>Digital Fragmentation.</strong> Your health, calendar, and
                chats are scattered across a dozen apps.
              </span>
            </li>
            <li>
              <span className="landing-pas-icon">
                <BellIcon width={28} height={28} />
              </span>
              <span>
                <strong>Notification Overload.</strong> You miss what matters
                because everything is shouting at once.
              </span>
            </li>
            <li>
              <span className="landing-pas-icon">
                <WrenchIcon width={28} height={28} />
              </span>
              <span>
                <strong>Complexity Fatigue.</strong> Automation tools feel like
                wiring a server, not living your life.
              </span>
            </li>
          </ul>
        </div>

        {/* Agitate */}
        <div className="landing-pas-agitate">
          <blockquote>
            Siri can't remember what you said five minutes ago.
            <br />
            <strong>What if your assistant was actually… smart?</strong>
          </blockquote>
        </div>

        {/* Solution */}
        <div className="landing-pas-solution">
          <h2 className="landing-section-label">The Solution</h2>
          <p>
            <strong>Swarmlet</strong> is a personal AI hub: plug in the tools you already use,
            and it watches your life signals — health, location, messages, home — to keep you
            organized <em>automatically</em>.
          </p>
        </div>
      </div>
    </section>
  );
}

