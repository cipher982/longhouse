/**
 * The "what it actually does" beat. Deliberately NOT the kicker→punchline→
 * three-cards template the rest of the page leans on — an asymmetric feature
 * row that leads with the crux (continue your sessions on the go) and keeps the
 * supporting facts subordinate, not co-equal boxes.
 */

export function KernelThesisSection() {
  return (
    <section id="how-it-works" className="landing-thesis">
      <div className="landing-section-inner landing-thesis-inner">
        <div className="landing-thesis-copy">
          <h2 className="landing-thesis-title">
            Leave your desk. Keep driving the session.
          </h2>
          <p className="landing-thesis-lead">
            Start a session at your desk and pick it back up from your phone or
            browser — read it, message it, steer it. Or launch a new one
            remotely and let it run while you&rsquo;re away.
          </p>

          <ul className="landing-thesis-points">
            <li>
              <strong>It just syncs.</strong> Install Longhouse and every Claude
              Code, Codex, Antigravity, and OpenCode session flows into one
              searchable timeline — live. No import, no setup.
            </li>
            <li>
              <strong>It stays on.</strong> Run it on your laptop to try it, then
              move the Runtime Host to a VPS, Mac mini, or homelab box so sessions
              keep running after you close the lid.
            </li>
          </ul>
        </div>

        <div className="landing-thesis-visual">
          <img
            src="/images/landing/phone-session.png"
            alt="A coding session open on a phone, ready to steer"
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
