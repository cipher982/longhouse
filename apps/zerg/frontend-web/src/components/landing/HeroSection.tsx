import { useState, useEffect } from "react";
import { SwarmLogo } from "../SwarmLogo";
import { useAuth } from "../../lib/auth";
import config from "../../lib/config";
import { HeroVisual } from "./HeroVisual";

interface HeroSectionProps {
  onScrollToScenarios: () => void;
}

export function HeroSection({ onScrollToScenarios }: HeroSectionProps) {
  const [showLogin, setShowLogin] = useState(false);
  const { login } = useAuth();
  const [isDevLoginLoading, setIsDevLoginLoading] = useState(false);

  const handleStartFree = () => {
    setShowLogin(true);
  };

  const handleDevLogin = async () => {
    setIsDevLoginLoading(true);
    try {
      const response = await fetch(`${config.apiBaseUrl}/auth/dev-login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });
      if (response.ok) {
        const data = await response.json();
        localStorage.setItem('zerg_jwt', data.access_token);
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
      <div className="landing-hero-content">
        <div className="landing-hero-visual-wrapper">
          <HeroVisual hideCore={true} />
          <SwarmLogo size={120} className="landing-hero-logo" />
        </div>

        <h1 className="landing-hero-headline">
          Your own <span className="gradient-text">super-Siri</span> for email,
          <br />health, chats, and home.
        </h1>

        <p className="landing-hero-subhead">
          Link your health data, location, inboxes, chats, and smart home into one brain.
          Get one place where your AI sees everything and quietly handles the annoying stuff.
        </p>

        <div className="landing-hero-ctas">
          <button className="btn-primary btn-lg landing-cta-main" onClick={handleStartFree}>
            Start Free
          </button>
          <button className="btn-ghost btn-lg" onClick={onScrollToScenarios}>
            See it in action ↓
          </button>
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
                  <button
                    className="btn-secondary btn-lg landing-dev-login"
                    onClick={handleDevLogin}
                    disabled={isDevLoginLoading}
                  >
                    {isDevLoginLoading ? 'Signing in...' : 'Dev Login (Local Only)'}
                  </button>
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
      try {
        await login(response.credential);
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
