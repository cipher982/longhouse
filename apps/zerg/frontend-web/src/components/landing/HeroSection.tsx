import { useState, useEffect } from "react";
import { SwarmLogo } from "../SwarmLogo";
import { Button } from "../ui";
import { useAuth } from "../../lib/auth";
import config from "../../lib/config";
import { AppScreenshotFrame } from "./AppScreenshotFrame";
import { InstallSection } from "./InstallSection";

interface HeroSectionProps {
  onScrollToHowItWorks: () => void;
  heroAnimationsEnabled: boolean;
}

export function HeroSection({ onScrollToHowItWorks, heroAnimationsEnabled: _heroAnimationsEnabled }: HeroSectionProps) {
  const [showLogin, setShowLogin] = useState(false);
  const [isDevLoginLoading, setIsDevLoginLoading] = useState(false);

  const handleGetStarted = () => {
    if (config.marketingOnly) {
      document.querySelector(".install-section")?.scrollIntoView({ behavior: "smooth" });
      return;
    }
    // Track CTA click
    if (window.LonghouseFunnel) {
      window.LonghouseFunnel.track('cta_clicked', { location: 'hero' });
    }

    // If auth is disabled (dev mode), go directly to timeline
    if (!config.authEnabled) {
      window.location.href = '/timeline';
      return;
    }

    setShowLogin(true);
    // Track modal opened
    if (window.LonghouseFunnel) {
      window.LonghouseFunnel.track('signup_modal_opened');
    }
  };

  const handleDevLogin = async () => {
    setIsDevLoginLoading(true);
    // Track signup submitted (dev login)
    if (window.LonghouseFunnel) {
      window.LonghouseFunnel.track('signup_submitted', { method: 'dev_login' });
    }
    try {
      const response = await fetch(`${config.apiBaseUrl}/auth/dev-login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include', // Cookie auth
      });
      if (response.ok) {
        const data = await response.json();
        // Cookie is set by server; no localStorage storage needed

        // Track signup completed and stitch visitor to user
        if (window.LonghouseFunnel) {
          const visitorId = window.LonghouseFunnel.getVisitorId();
          window.LonghouseFunnel.track('signup_completed', { method: 'dev_login' });

          // Stitch visitor to user (fire and forget)
          fetch(`${config.apiBaseUrl}/funnel/stitch-visitor`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({
              visitor_id: visitorId,
              user_id: data.user_id || 'dev_user'
            })
          }).catch(() => {});
        }

        window.location.href = '/timeline';
      }
    } catch (error) {
      console.error('Dev login failed:', error);
    } finally {
      setIsDevLoginLoading(false);
    }
  };

  return (
    <section className="landing-hero">
      <div className="landing-hero-split">
        {/* Left: Text content */}
        <div className="landing-hero-text">
          <div className="landing-hero-badge">
            <span className="landing-hero-badge-dot" />
            Free during beta
          </div>

          <h1 className="landing-hero-headline">
            Close your laptop. <span className="gradient-text">Keep coding.</span>
          </h1>

          <p className="landing-hero-subhead">
            Your AI agents run in the cloud. Resume from any device. Never lose a session.
          </p>

          <p className="landing-hero-note">
            Works with Claude Code today. Codex, Cursor, Gemini coming soon.
          </p>

          {/* Install command section - primary CTA */}
          <InstallSection className="landing-hero-install" />

          <div className="landing-hero-ctas">
            <Button variant="ghost" size="lg" className="landing-cta-text" onClick={onScrollToHowItWorks}>
              See How It Works <span className="landing-cta-arrow">↓</span>
            </Button>
            <Button variant="ghost" size="lg" className="landing-cta-text landing-cta-main" onClick={handleGetStarted}>
              Sign In
            </Button>
          </div>
        </div>

        {/* Right: Product screenshot */}
        <div className="landing-hero-visual">
          <AppScreenshotFrame
            src="/images/landing/dashboard-preview.png"
            alt="Longhouse session timeline showing Claude Code sessions"
            title="Longhouse"
            aspectRatio="4/3"
            showChrome={true}
            className="landing-hero-screenshot"
          />
        </div>
      </div>

      {/* Login Modal */}
      {!config.marketingOnly && showLogin && (
        <div className="landing-login-overlay" onClick={() => setShowLogin(false)}>
          <div className="landing-login-modal" onClick={(e) => e.stopPropagation()}>
            <button className="landing-login-close" onClick={() => setShowLogin(false)}>
              ×
            </button>
            <SwarmLogo size={48} className="landing-login-logo" />
            <h2>Welcome to Longhouse</h2>
            <p className="landing-login-subtext">Sign in to access your session timeline</p>

            <div className="landing-login-buttons">
              <GoogleSignInButtonWrapper />

              {config.isDevelopment && (
                <>
                  <div className="landing-login-divider">
                    <span>or</span>
                  </div>
                  <Button
                    variant="success"
                    size="lg"
                    className="landing-dev-login"
                    onClick={handleDevLogin}
                    disabled={isDevLoginLoading}
                  >
                    {isDevLoginLoading ? 'Signing in...' : 'Dev Login (Local Only)'}
                  </Button>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

// Wrapper to handle Google Sign-In button
function GoogleSignInButtonWrapper() {
  const { login } = useAuth();
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    const handleCredentialResponse = async (response: { credential: string }) => {
      setIsLoading(true);

      // Track signup submitted (Google OAuth)
      if (window.LonghouseFunnel) {
        window.LonghouseFunnel.track('signup_submitted', { method: 'google_oauth' });
      }

      try {
        await login(response.credential);

        // Track signup completed and stitch visitor to user
        if (window.LonghouseFunnel) {
          const visitorId = window.LonghouseFunnel.getVisitorId();
          window.LonghouseFunnel.track('signup_completed', { method: 'google_oauth' });

          // Stitch visitor to user (fire and forget)
          // Use email as user_id since login() doesn't return user object
          fetch(config.apiBaseUrl + '/funnel/stitch-visitor', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              visitor_id: visitorId,
              user_id: 'google_oauth_user'  // Placeholder, will be backfilled from auth token
            })
          }).catch(() => {});
        }

        window.location.href = '/timeline';
      } catch (error) {
        console.error('Login failed:', error);
      } finally {
        setIsLoading(false);
      }
    };

    // Initialize Google Sign-In
    const script = document.createElement('script');
    script.src = 'https://accounts.google.com/gsi/client';
    script.async = true;
    script.defer = true;
    document.head.appendChild(script);

    script.onload = () => {
      if (window.google?.accounts?.id) {
        window.google.accounts.id.initialize({
          client_id: config.googleClientId,
          callback: handleCredentialResponse,
        });
        const buttonDiv = document.getElementById('landing-google-signin');
        if (buttonDiv) {
          window.google.accounts.id.renderButton(buttonDiv, {
            theme: 'filled_black',
            size: 'large',
          });
        }
      }
    };

    return () => {
      script.remove();
    };
  }, [login]);

  return (
    <div className="landing-google-signin-wrapper">
      <div id="landing-google-signin" />
      {isLoading && <div className="landing-signin-loading">Signing in...</div>}
    </div>
  );
}
