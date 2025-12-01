export function PASSection() {
  return (
    <section className="landing-pas">
      <div className="landing-section-inner">
        {/* Problem */}
        <div className="landing-pas-problem">
          <h2 className="landing-section-label">The Problem</h2>
          <ul className="landing-pas-bullets">
            <li>
              <span className="landing-pas-icon">📱</span>
              Your health is in one app, your calendar in another, your chats in three more.
            </li>
            <li>
              <span className="landing-pas-icon">🔔</span>
              You miss things because everything is shouting at you in different places.
            </li>
            <li>
              <span className="landing-pas-icon">🔧</span>
              Automation tools make you feel like you're wiring a server, not living your life.
            </li>
          </ul>
        </div>

        {/* Agitate */}
        <div className="landing-pas-agitate">
          <blockquote>
            Most "AI platforms" are built for teams, dashboards, and managers.
            <br />
            <strong>You just want your own stuff taken care of</strong> — without needing a project plan.
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

