import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { SwarmLogo } from "../../components/SwarmLogo";
import { usePublicPageScroll } from "../../hooks/usePublicPageScroll";
import "../../styles/docs.css";

const NAV_SECTIONS = [
  {
    label: "Getting Started",
    items: [
      { to: "/docs", label: "Overview", exact: true },
      { to: "/docs/quickstart", label: "Quick Start" },
      { to: "/docs/search", label: "Search & Recall" },
    ],
  },
  {
    label: "Remote Control",
    items: [
      { to: "/docs/remote-control", label: "Control After Launch" },
      { to: "/docs/cli", label: "CLI Reference" },
      { to: "/docs/api", label: "Machine API" },
    ],
  },
  {
    label: "Setup",
    items: [
      { to: "/docs/integrations", label: "Integrations" },
      { to: "/docs/configuration", label: "Configuration" },
    ],
  },
];

export default function DocsLayout() {
  const location = useLocation();
  usePublicPageScroll();

  return (
    <div className="docs-page">
      <header className="docs-header">
        <div className="docs-header-inner">
          <Link to="/" className="docs-brand">
            <SwarmLogo size={24} />
            <span className="docs-brand-name">Longhouse</span>
            <span className="docs-brand-sep">/</span>
            <span className="docs-brand-section">Docs</span>
          </Link>
          <div className="docs-header-links">
            <a
              href="https://github.com/cipher982/longhouse"
              target="_blank"
              rel="noopener noreferrer"
              className="docs-header-link"
            >
              GitHub
            </a>
            <a
              href="https://discord.gg/mekG4Pp5q"
              target="_blank"
              rel="noopener noreferrer"
              className="docs-header-link"
            >
              Discord
            </a>
          </div>
        </div>
      </header>

      <div className="docs-body">
        <nav className="docs-sidebar">
          {NAV_SECTIONS.map((section) => (
            <div key={section.label} className="docs-sidebar-group">
              <h4 className="docs-sidebar-label">{section.label}</h4>
              {section.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.exact}
                  className={({ isActive }) =>
                    `docs-sidebar-link ${isActive ? "active" : ""}`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        <main className="docs-content">
          <Outlet />
          <DocsFooterNav currentPath={location.pathname} />
        </main>
      </div>
    </div>
  );
}

/* ---- prev / next navigation ---- */

const FLAT_NAV = NAV_SECTIONS.flatMap((s) => s.items);

function DocsFooterNav({ currentPath }: { currentPath: string }) {
  const idx = FLAT_NAV.findIndex((n) => n.to === currentPath);
  const prev = idx > 0 ? FLAT_NAV[idx - 1] : null;
  const next = idx < FLAT_NAV.length - 1 ? FLAT_NAV[idx + 1] : null;

  if (!prev && !next) return null;

  return (
    <nav className="docs-footer-nav">
      {prev ? (
        <Link to={prev.to} className="docs-footer-nav-link prev">
          <span className="docs-footer-nav-dir">Previous</span>
          <span className="docs-footer-nav-title">{prev.label}</span>
        </Link>
      ) : (
        <span />
      )}
      {next ? (
        <Link to={next.to} className="docs-footer-nav-link next">
          <span className="docs-footer-nav-dir">Next</span>
          <span className="docs-footer-nav-title">{next.label}</span>
        </Link>
      ) : (
        <span />
      )}
    </nav>
  );
}
