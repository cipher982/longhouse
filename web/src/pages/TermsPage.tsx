import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { usePageMeta } from "../hooks/usePageMeta";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import "../styles/info-pages.css";

export default function TermsPage() {
  const currentYear = new Date().getFullYear();

  usePublicPageScroll();
  usePageMeta({
    title: "Terms of Service - Longhouse",
    description:
      "The terms for the hosted Longhouse service: what we provide, what we expect from you, billing, data retention, and the limits of what we promise.",
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
        <h1 className="info-page-title">Terms of Service</h1>
        <p className="info-page-subtitle">
          The deal between you and the hosted Longhouse service.
        </p>
        <p className="info-page-updated">Last updated: August 24, 2026</p>

        <div className="legal-content">
          <h2>What This Covers</h2>
          <p>
            These terms cover the hosted service at longhouse.ai — the account, the instance we run
            for you, and the apps that talk to it. Using it means you accept them.
          </p>
          <p>
            The Longhouse software itself is open source under Apache-2.0. That license covers the
            code, and it disclaims warranties on the code. It says nothing about a service we run,
            which is why this page exists.
          </p>

          <h2>Your Account</h2>
          <p>
            Give us a real email address and keep your credentials and device tokens to yourself.
            Anything done with your account or your tokens is treated as done by you. Tell us
            promptly if you think either has leaked, and revoke the token.
          </p>
          <p>
            You need to be old enough to enter a contract where you live. One human, one account.
          </p>

          <h2>Your Content</h2>
          <p>
            Your sessions and transcripts are yours. You give us permission to store, transmit, and
            process them for the sole purpose of running the service for you — including sending
            excerpts to a model provider to generate titles and summaries, which our{" "}
            <Link to="/privacy">Privacy Policy</Link> describes in detail.
          </p>
          <p>
            Only send us content you have the right to send. Agent transcripts routinely capture
            keys, credentials, and other people's data; you are the one who decides what your agents
            record and what gets shipped to us.
          </p>

          <h2>What We Ask</h2>
          <p>Don't use the hosted service to:</p>
          <ul>
            <li>break the law, or help someone else break it</li>
            <li>store or process data you aren't allowed to hand to a third party</li>
            <li>attack, overload, or probe the service or other tenants</li>
            <li>resell the hosted service as your own</li>
          </ul>
          <p>
            Testing your own instance for vulnerabilities is fine and welcome — tell us what you
            find, per the disclosure process on our <Link to="/security">Security</Link> page.
          </p>

          <h2>Billing</h2>
          <p>
            Self-hosting is free. The hosted plan is $5 a month, billed through Stripe until you
            cancel. Cancel anytime from the billing portal; you keep access through the period you
            already paid for, and we don't prorate a partial month. If something goes wrong on our
            end, email us — we would rather refund you than argue.
          </p>

          <h2>Data Retention and Deletion</h2>
          <p>
            We keep your sessions until you delete them or ask us to. Nothing expires on a timer.
            Deletion requests go to <a href="mailto:support@longhouse.ai">support@longhouse.ai</a>{" "}
            from the account's email address, and we complete them within 7 days. Copies can survive
            in off-site backups until those rotate out. Full detail is in the{" "}
            <Link to="/privacy">Privacy Policy</Link>.
          </p>
          <p>
            If you close your account, we delete your instance and its data. If we ever have to shut
            the hosted service down, we will email you first and give you time to export and to
            self-host instead.
          </p>

          <h2>What We Promise, and What We Don't</h2>
          <p>
            Longhouse is a small operation, and the hosted service is provided as is. There is no
            uptime guarantee, no support SLA, and no promise that a feature that works today works
            the same way next month. We back the hosted database up off-site, but you should not
            treat Longhouse as the only copy of anything you cannot lose.
          </p>
          <p>
            To the extent the law allows, we are not liable for indirect or consequential damages,
            and our total liability is limited to what you paid us in the twelve months before the
            claim.
          </p>
          <p>
            Longhouse can start, steer, and stop agents on machines you connect. Those agents run as
            you, with your permissions, and can change or delete files. You are responsible for what
            you tell them to do.
          </p>

          <h2>Ending It</h2>
          <p>
            You can stop using the service at any time, and close your account whenever you want by
            emailing us — there is no self-serve close button yet, so the request goes through{" "}
            <a href="mailto:support@longhouse.ai">support@longhouse.ai</a>. We can suspend or
            close an account that breaks these terms, or that puts the service or other users at
            risk. Except for abuse severe enough to need an immediate stop, we will tell you why and
            give you a chance to get your data out.
          </p>

          <h2>Changes</h2>
          <p>
            If we change these terms, we update the date at the top of this page. For a change that
            actually affects you, we email the address on your account before it takes effect.
          </p>

          <h2>Contact</h2>
          <p>
            Email <a href="mailto:support@longhouse.ai">support@longhouse.ai</a> or join our{" "}
            <a href="https://discord.gg/mekG4Pp5q" target="_blank" rel="noopener noreferrer">Discord</a>.
          </p>
        </div>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Longhouse. All rights reserved.</p>
      </footer>
    </div>
  );
}
