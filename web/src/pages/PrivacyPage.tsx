import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { usePageMeta } from "../hooks/usePageMeta";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import "../styles/info-pages.css";

export default function PrivacyPage() {
  const currentYear = new Date().getFullYear();

  usePublicPageScroll();
  usePageMeta({
    title: "Privacy Policy - Longhouse",
    description:
      "What Longhouse stores, which companies it sends your data to, how long it keeps it, and how to get it deleted.",
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
        <h1 className="info-page-title">Privacy Policy</h1>
        <p className="info-page-subtitle">
          How we handle your data.
        </p>
        <p className="info-page-updated">Last updated: August 25, 2026</p>

        <div className="legal-content">
          <p>
            This covers the hosted service at longhouse.ai. If you self-host, none of your
            sessions reach us — you are the operator, and the only third parties involved are
            the ones you configure yourself.
          </p>

          <h2>What We Collect</h2>

          <h3>Account Information</h3>
          <p>
            When you create an account, we store your email address and display name
            to identify your account.
          </p>

          <h3>Your Content</h3>
          <p>
            Your agent sessions and conversation history are stored to provide the service.
            This data is associated with your account.
          </p>
          <p>
            We store transcripts as your agents produced them. Longhouse does not strip secrets
            out of what it stores, so if a session printed an API key, an SSH key, or a password,
            that value is stored too. Treat your session history as sensitive, because it is.
          </p>

          <h3>Integration Credentials</h3>
          <p>
            When you connect integrations, your credentials are stored encrypted.
          </p>

          <h2>What We Don't Do</h2>
          <ul>
            <li><strong>We don't train AI on your data.</strong> Your conversations are yours.</li>
            <li><strong>We don't sell your data.</strong></li>
          </ul>
          <p>
            <strong>We don't send your transcripts to a model provider unless you turn it on.</strong>{" "}
            Longhouse can use one to generate session titles and summaries, and that is off by
            default. While it is off, no transcript content leaves your instance: titles come from
            the first message of the session, or from the project and branch it ran in. We default
            it off because transcripts contain API keys, SSH keys, and database URLs, and we would
            rather not hand those to another company on your behalf.
          </p>

          <h2>Where Your Data Goes</h2>
          <p>
            The hosted service hands data to these companies. A self-hosted instance uses only the
            ones you configure.
          </p>
          <ul>
            <li>
              <strong>OpenRouter</strong>, routing to <strong>DeepSeek</strong> — excerpts of your
              session transcripts, for generating titles and summaries, and only if you turn that
              on. It is off by default, and while it is off we send them nothing. There is no
              self-serve switch yet: email us to turn it on, and email us to turn it back off.
            </li>
            <li>
              <strong>Google</strong> — your email address and profile name, if you sign in with
              Google.
            </li>
            <li>
              <strong>Stripe</strong> — your email address and your payment details, for paid plans.
              Card numbers go to Stripe, not to us.
            </li>
            <li>
              <strong>Amazon SES</strong> — your email address and the contents of mail we send you,
              such as email verification and alerts.
            </li>
            <li>
              <strong>Apple (APNs)</strong> — if you use the iOS app with notifications enabled, the
              session title and summary travel in the push payload.
            </li>
            <li>
              <strong>Backblaze B2</strong> — off-site backups of the hosted database.
            </li>
            <li>
              <strong>Cloudflare</strong> — DNS and inbound mail routing for longhouse.ai.
            </li>
            <li>
              <strong>Umami analytics</strong>, which we run ourselves — page views on our marketing,
              signup, and billing pages, plus a session recorder that replays interactions on those
              pages. It is not loaded on the hosted instances that hold your sessions.
            </li>
          </ul>

          <h2>How Long We Keep It</h2>
          <p>
            Sessions and transcripts stay until you delete them or ask us to delete them. Nothing
            expires on a timer today.
          </p>
          <p>
            Deleting removes data from the live service. Copies can survive in off-site backups
            until those backups rotate out, and we don't restore deleted data from them.
          </p>
          <p>
            Account and billing records are kept while the account exists, and billing records for
            as long as Stripe and tax rules require after it closes.
          </p>

          <h2>Deleting Your Data</h2>
          <p>Deletion is partly manual right now. Concretely:</p>
          <ul>
            <li>
              <strong>In the app</strong> — revoke device tokens under Settings, and delete any share
              links you created.
            </li>
            <li>
              <strong>By email</strong> — to delete a session, your whole history, or your entire
              account, email <a href="mailto:support@longhouse.ai">support@longhouse.ai</a> from the
              address on the account. We do it and confirm back within 7 days.
            </li>
          </ul>
          <p>
            There is no self-serve "delete my account" button yet. We would rather tell you that
            than point you at a settings page that doesn't have one.
          </p>

          <h2>Your Other Rights</h2>
          <ul>
            <li><strong>Access</strong> your data through the timeline</li>
            <li><strong>Revoke</strong> integrations at any time</li>
          </ul>

          <h2>Cookies</h2>
          <p>
            We use essential cookies to keep you logged in and remember preferences. Analytics on our
            public pages is described under "Where Your Data Goes".
          </p>

          <h2>Contact</h2>
          <p>
            Questions? Join our <a href="https://discord.gg/mekG4Pp5q" target="_blank" rel="noopener noreferrer">Discord</a> or
            email <a href="mailto:support@longhouse.ai">support@longhouse.ai</a>
          </p>
          <p>
            See also our <Link to="/terms">Terms of Service</Link> and <Link to="/security">Security</Link> page.
          </p>
        </div>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Longhouse. All rights reserved.</p>
      </footer>
    </div>
  );
}
