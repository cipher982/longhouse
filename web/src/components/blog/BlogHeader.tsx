import { useNavigate } from "react-router-dom";
import config from "../../lib/config";
import { LandingHeader, type LandingNavLink } from "../landing/LandingHeader";

const BLOG_NAV_LINKS: LandingNavLink[] = [
  { label: "Home", href: "/" },
  { label: "Docs", href: "/docs" },
];

export function BlogHeader() {
  const navigate = useNavigate();

  const handleSignIn = () => {
    if (config.demoMode) {
      window.location.href = "https://control.longhouse.ai";
      return;
    }
    navigate("/login");
  };

  return (
    <LandingHeader
      navLinks={BLOG_NAV_LINKS}
      onSignIn={handleSignIn}
      onGetStarted={() => window.location.assign("/#landing-install")}
    />
  );
}
