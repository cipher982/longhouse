import { useState, useEffect } from "react";
import { SwarmLogo } from "../SwarmLogo";
import { Button } from "../ui";
import { useAuth } from "../../lib/auth";
import config from "../../lib/config";
import { AppScreenshotFrame } from "./AppScreenshotFrame";

interface HeroSectionProps {
  onScrollToHowItWorks: () => void;
  heroAnimationsEnabled: boolean;
}

export function HeroSection({ onScrollToHowItWorks, heroAnimationsEnabled: _heroAnimationsEnabled }: HeroSectionProps) {
  const [showLogin, setShowLogin] = useState(false);
  const [isDevLoginLoading, setIsDevLoginLoading] = useState(false);

  const handleGetStarted = () => {
    // Track CTA click
    if (window.SwarmletFunnel) {
      window.SwarmletFunnel.track('cta_clicked', { location: 'hero' });
    }

    // If auth is disabled (dev mode), go directly to dashboard
    if (!config.authEnabled) {
      window.location.href = '/dashboard';
      return;
    }

    setShowLogin(true);
    // Track modal opened
    if (window.SwarmletFunnel) {
      window.SwarmletFunnel.track('signup_modal_opened');
    }
  };

  const handleDevLogin = async () => {
    setIsDevLoginLoading(true);
    // Track signup submitted (dev login)
    if (window.SwarmletFunnel) {
      window.SwarmletFunnel.track('signup_submitted', { method: 'dev_login' });
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
        if (window.SwarmletFunnel) {
          const visitorId = window.SwarmletFunnel.getVisitorId();
          window.SwarmletFunnel.track('signup_completed', { method: 'dev_login' });

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

        window.location.href = '/dashboard';
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
            Your AI That Actually <span className="gradient-text">Knows You</span>
          </h1>

          <p className="landing-hero-subhead">
            Connect your health, calendar, and inbox.
            Get an AI that remembers everything and acts on it.
          </p>

          <div className="landing-hero-ctas">
            <Button variant="primary" size="lg" className="landing-cta-main" onClick={handleGetStarted}>
              Get My AI Assistant
            </Button>
            <Button variant="ghost" size="lg" className="landing-cta-text" onClick={onScrollToHowItWorks}>
              See How It Works <span className="landing-cta-arrow">↓</span>
            </Button>
          </div>
        </div>

        {/* Right: Product screenshot */}
        <div className="landing-hero-visual">
          <AppScreenshotFrame
            src="/images/landing/chat-preview.png"
            alt="Swarmlet AI chat interface"
            title="Swarmlet"
            aspectRatio="4/3"
            showChrome={true}
            className="landing-hero-screenshot"
          />
        </div>
      </div>

      {/* Login Modal */}
      {showLogin && (
        <div className="landing-login-overlay" onClick={() => setShowLogin(false)}>
          <div className="landing-login-modal" onClick={(e) => e.stopPropagation()}>
            <button className="landing-login-close" onClick={() => setShowLogin(false)}>
              ×
            </button>
            <SwarmLogo size={48} className="landing-login-logo" />
            <h2>Welcome to Swarmlet</h2>
            <p className="landing-login-subtext">Sign in to start building your personal AI</p>

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
      if (window.SwarmletFunnel) {
        window.SwarmletFunnel.track('signup_submitted', { method: 'google_oauth' });
      }

      try {
        await login(response.credential);

        // Track signup completed and stitch visitor to user
        if (window.SwarmletFunnel) {
          const visitorId = window.SwarmletFunnel.getVisitorId();
          window.SwarmletFunnel.track('signup_completed', { method: 'google_oauth' });

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

        window.location.href = '/dashboard';
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
