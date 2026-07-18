import { useState, useEffect } from "react";
import { SwarmLogo } from "../SwarmLogo";
import { Button } from "../ui/Button";

export type LandingNavLink = {
  label: string;
  href: string;
  external?: boolean;
};

interface LandingHeaderProps {
  onSignIn?: () => void;
  onGetStarted?: () => void;
  navLinks?: LandingNavLink[];
}

const DEFAULT_NAV_LINKS: LandingNavLink[] = [
  { label: "How it works", href: "#how-it-works" },
  { label: "Machine surface", href: "#surface" },
  { label: "Providers", href: "#providers" },
  { label: "Blog", href: "/blog" },
  { label: "Docs", href: "/docs" },
];

export function LandingHeader({ onSignIn, onGetStarted, navLinks = DEFAULT_NAV_LINKS }: LandingHeaderProps) {
  const [isScrolled, setIsScrolled] = useState(false);
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);

  useEffect(() => {
    const handleScroll = () => {
      setIsScrolled(window.scrollY > 20);
    };

    window.addEventListener("scroll", handleScroll, { passive: true });
    return () => window.removeEventListener("scroll", handleScroll);
  }, []);

  const handleNavClick = (href: string, external?: boolean) => {
    if (external) {
      window.open(href, "_blank", "noopener,noreferrer");
    } else if (href.startsWith("#")) {
      const element = document.querySelector(href);
      if (element) {
        element.scrollIntoView({ behavior: "smooth" });
      }
    } else {
      window.location.assign(href);
    }
    setIsMobileMenuOpen(false);
  };

  return (
    <header className={`landing-header ${isScrolled ? "landing-header--scrolled" : ""}`}>
      <div className="landing-header-inner">
        {/* Logo + Wordmark */}
        <a href="/" className="landing-header-brand">
          <SwarmLogo size={32} className="landing-header-logo" />
          <span className="landing-header-wordmark">Longhouse</span>
        </a>

        {/* Desktop Nav */}
        <nav className="landing-header-nav">
          {navLinks.map((link) => (
            <button
              key={link.label}
              className="landing-header-nav-link"
              onClick={() => handleNavClick(link.href, link.external)}
              type="button"
            >
              {link.label}
            </button>
          ))}
        </nav>

        {/* Desktop Actions */}
        <div className="landing-header-actions">
          <Button variant="ghost" size="sm" onClick={onSignIn}>
            Sign In
          </Button>
          <Button variant="primary" size="sm" onClick={onGetStarted}>
            Self-Host Free
          </Button>
        </div>

        {/* Mobile Menu Toggle */}
        <button
          className="landing-header-menu-toggle"
          onClick={() => setIsMobileMenuOpen(!isMobileMenuOpen)}
          aria-label={isMobileMenuOpen ? "Close menu" : "Open menu"}
          aria-expanded={isMobileMenuOpen}
          type="button"
        >
          <span className={`hamburger ${isMobileMenuOpen ? "hamburger--open" : ""}`}>
            <span className="hamburger-line" />
            <span className="hamburger-line" />
            <span className="hamburger-line" />
          </span>
        </button>
      </div>

      {/* Mobile Menu */}
      <div
        className={`landing-header-mobile-menu ${isMobileMenuOpen ? "landing-header-mobile-menu--open" : ""}`}
        aria-hidden={!isMobileMenuOpen}
      >
        <nav className="landing-header-mobile-nav">
          {navLinks.map((link) => (
            <button
              key={link.label}
              className="landing-header-mobile-link"
              onClick={() => handleNavClick(link.href, link.external)}
              type="button"
              tabIndex={isMobileMenuOpen ? 0 : -1}
            >
              {link.label}
            </button>
          ))}
        </nav>
        <div className="landing-header-mobile-actions">
          <Button variant="ghost" size="md" onClick={() => { onSignIn?.(); setIsMobileMenuOpen(false); }} tabIndex={isMobileMenuOpen ? 0 : -1}>
            Sign In
          </Button>
          <Button variant="primary" size="md" onClick={() => { onGetStarted?.(); setIsMobileMenuOpen(false); }} tabIndex={isMobileMenuOpen ? 0 : -1}>
            Self-Host Free
          </Button>
        </div>
      </div>
    </header>
  );
}
