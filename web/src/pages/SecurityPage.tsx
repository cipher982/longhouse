import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { ShieldIcon, LockIcon, TrashIcon, KeyIcon } from "../components/icons";
import { usePageMeta } from "../hooks/usePageMeta";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import "../styles/info-pages.css";

export default function SecurityPage() {
  const currentYear = new Date().getFullYear();

  usePublicPageScroll();
  usePageMeta({
    title: "Security - Longhouse",
    description:
      "How Longhouse protects your data: TLS on the hosted service, secure authentication, encrypted credentials, revocable device tokens, how long we keep transcripts, and responsible disclosure.",
  });

  return (
    <div className="info-page">
      <header className="info-page-header">
        <div className="info-page-header-inner">
          <Link to="/" className="info-page-back">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
            Back to Home
          </Link>
          <Link to="/" className="info-page-brand">
            <SwarmLogo size={28} />
            <span className="info-page-brand-name">Longhouse</span>
          </Link>
        </div>
      </header>

      <main className="info-page-content">
        <h1 className="info-page-title">Security</h1>
        <p className="info-page-subtitle">
          How we approach security and protect your data.
        </p>

        <div className="security-highlights">
          <div className="security-highlight">
            <div className="security-highlight-icon">
              <ShieldIcon width={24} height={24} />
            </div>
            <h3>TLS on Hosted</h3>
            <p>longhouse.ai is served over HTTPS</p>
          </div>
          <div className="security-highlight">
            <div className="security-highlight-icon">
              <LockIcon width={24} height={24} />
            </div>
            <h3>Authentication</h3>
            <p>Password or OAuth sign-in</p>
          </div>
          <div className="security-highlight">
            <div className="security-highlight-icon">
              <KeyIcon width={24} height={24} />
            </div>
            <h3>Secure Credentials</h3>
            <p>Integration credentials encrypted</p>
          </div>
          <div className="security-highlight">
            <div className="security-highlight-icon">
              <TrashIcon width={24} height={24} />
            </div>
            <h3>Revocable Access</h3>
            <p>Revoke a device token anytime</p>
          </div>
        </div>

        <div className="legal-content">
          <h2>Transport</h2>
          <p>
            The hosted service is served over HTTPS. Everything your machines send it — device
            tokens, transcripts, control commands — travels over that TLS connection.
          </p>
          <p>
            Self-hosting is your call, and Longhouse will not add TLS for you. If you point a machine
            at a plain http:// address that isn't loopback, its device token and its transcripts
            cross your network in the clear. Put the instance behind TLS, or keep it on a private
            overlay network you trust.
          </p>

          <h2>Authentication</h2>
          <p>
            Self-hosted instances use password authentication by default. The hosted service
            supports OAuth providers. Longhouse never stores third-party passwords.
          </p>

          <h2>Integration Credentials</h2>
          <p>
            When you connect integrations (Slack, Discord, GitHub, etc.), your credentials are stored
            encrypted and only used to connect to those services on your behalf.
          </p>

          <h2>What Sessions Contain</h2>
          <p>
            Longhouse stores agent transcripts as they were produced. It does not scrub secrets out
            of them, so anything your agent printed — keys, tokens, passwords — is in the archive.
            Anyone who can read your Longhouse account can read those values.
          </p>

          <h2>Retention and Deletion</h2>
          <p>
            We keep your transcripts until you delete them. Nothing expires on a timer, and we
            don't thin old history to save space.
          </p>
          <p>
            There is no delete button in the app yet, so you email us and we run it within 7 days.
            The deletion itself is immediate: it removes the session from the live service — the
            catalog rows, the search index and its embeddings, and the stored transcript bytes. A
            real delete, not a flag that hides the session from view. The server reports what it
            removed, and it names anything it could not reach instead of claiming a clean sweep:
            today that always includes an older archive copy of the transcript on the server, and
            any media file another session still points at.
          </p>
          <p>
            Backups are the honest exception. We back the hosted database up off-site, encrypted,
            and a snapshot taken before you deleted something still holds it until that snapshot
            ages out — daily copies for two weeks, then thinned weekly, monthly, and yearly copies
            beyond that. We never restore deleted data back into the service.
          </p>
          <p>
            So deleting a session is not the same as un-leaking a secret. If a transcript ever
            held a live key, token, or password, rotate it. Rotation is the only thing that
            actually revokes it.
          </p>

          <h2>Your Controls</h2>
          <p>You have control over your data:</p>
          <ul>
            <li><strong>View</strong> - See your sessions and timeline data</li>
            <li><strong>Revoke</strong> - Revoke a device token, or disconnect an integration and delete its credentials</li>
            <li><strong>Delete</strong> - Delete a session or your whole history. There is no delete button in the app yet, so email <a href="mailto:support@longhouse.ai">support@longhouse.ai</a> from your account address and we run it</li>
          </ul>

          <h2>Responsible Disclosure</h2>
          <p>
            If you discover a security vulnerability, please report it to us:
          </p>
          <ul>
            <li>Email: <a href="mailto:support@longhouse.ai">support@longhouse.ai</a></li>
            <li>Include details and steps to reproduce</li>
            <li>Allow reasonable time for us to address it</li>
          </ul>

          <div className="security-contact">
            <h3>Questions?</h3>
            <p>
              For security questions, join our <a href="https://discord.gg/mekG4Pp5q" target="_blank" rel="noopener noreferrer">Discord</a> or
              email <a href="mailto:support@longhouse.ai">support@longhouse.ai</a>
            </p>
            <p>
              For privacy questions, see our <Link to="/privacy">Privacy Policy</Link>. The hosted
              service is covered by our <Link to="/terms">Terms of Service</Link>.
            </p>
          </div>
        </div>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Longhouse. All rights reserved.</p>
      </footer>
    </div>
  );
}
