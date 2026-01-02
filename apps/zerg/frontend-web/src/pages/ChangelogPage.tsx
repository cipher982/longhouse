import { useEffect } from "react";
import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import "../styles/info-pages.css";

export default function ChangelogPage() {
  const currentYear = new Date().getFullYear();

  usePublicPageScroll();

  useEffect(() => {
    document.title = "Changelog - Swarmlet";
    const metaDescription = document.querySelector('meta[name="description"]');
    if (metaDescription) {
      metaDescription.setAttribute('content', 'Track Swarmlet\'s development progress. See new features, improvements, and fixes as we build the platform.');
    }
  }, []);

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
            <span className="info-page-brand-name">Swarmlet</span>
          </Link>
        </div>
      </header>

      <main className="info-page-content">
        <h1 className="info-page-title">Changelog</h1>
        <p className="info-page-subtitle">
          Track our progress as we build Swarmlet.
        </p>

        <div className="docs-section" style={{ textAlign: "center", padding: "var(--space-16) 0" }}>
          <p style={{ fontSize: "1.125rem", color: "var(--text-secondary)" }}>
            We're currently in beta. A detailed changelog will be available once we reach our first stable release.
          </p>
          <p style={{ marginTop: "var(--space-6)" }}>
            Follow development on{" "}
            <a href="https://github.com/cipher982/zerg" target="_blank" rel="noopener noreferrer">
              GitHub
            </a>
            {" "}to see commits and releases in real time.
          </p>
        </div>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Swarmlet. All rights reserved.</p>
      </footer>
    </div>
  );
}
