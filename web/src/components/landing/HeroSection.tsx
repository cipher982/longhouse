import { useState } from "react";
import { Button } from "../ui";
import config from "../../lib/config";
import { trackAcquisitionEvent } from "../../lib/analytics";
import { useNavigate } from "react-router-dom";

const INSTALL_COMMAND = "curl -fsSL https://get.longhouse.ai/install.sh | bash";
const MAC_DOWNLOAD_URL = "/download/macos";

export function HeroSection() {
  const navigate = useNavigate();
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(INSTALL_COMMAND);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = INSTALL_COMMAND;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    trackAcquisitionEvent("install_command_copy", {
      surface: "landing",
      placement: "hero",
      method: "curl",
    });
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleMacDownloadClick = () => {
    trackAcquisitionEvent("mac_download_click", {
      surface: "landing",
      placement: "hero",
      method: "direct_download",
    });
  };

  return (
    <section className="landing-hero" id="landing-install">
      <div className="landing-hero-content">
        <p className="landing-hero-kicker">Self-hosted session control</p>

        <h1 className="landing-hero-headline">
          One timeline for every{" "}
          <span className="gradient-text">AI coding session.</span>
        </h1>

        <p className="landing-hero-subhead">
          Longhouse captures every Claude, Codex, Antigravity, and OpenCode session from your
          machines — searchable on the web, one glance away on your phone.
        </p>

        {/* ── Install paths ── */}
        <div className="hero-install">
          {/* Terminal path */}
          <div className="hero-install-terminal">
            <button
              type="button"
              className="hero-install-cmd"
              onClick={handleCopy}
              aria-label={`Copy install command: ${INSTALL_COMMAND}`}
            >
              <span className="hero-install-prompt" aria-hidden="true">
                $
              </span>
              <code className="hero-install-text">{INSTALL_COMMAND}</code>
              <span className={`hero-install-copy ${copied ? "copied" : ""}`}>
                {copied ? (
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                    <path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z" />
                  </svg>
                ) : (
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                    <path d="M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 010 1.5h-1.5a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-1.5a.75.75 0 011.5 0v1.5A1.75 1.75 0 019.25 16h-7.5A1.75 1.75 0 010 14.25v-7.5z" />
                    <path d="M5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0114.25 11h-7.5A1.75 1.75 0 015 9.25v-7.5zm1.75-.25a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-7.5a.25.25 0 00-.25-.25h-7.5z" />
                  </svg>
                )}
              </span>
            </button>
            <p className="hero-install-note">
              Best for agents, automation, Linux, or WSL.
            </p>
          </div>

          {/* Divider */}
          <div className="hero-install-divider" aria-hidden="true">
            <span className="hero-install-divider-line" />
            <span className="hero-install-divider-label">or</span>
            <span className="hero-install-divider-line" />
          </div>

          {/* Mac download path */}
          <a
            href={MAC_DOWNLOAD_URL}
            className="hero-install-mac"
            onClick={handleMacDownloadClick}
          >
            <svg
              className="hero-install-mac-icon"
              width="20"
              height="24"
              viewBox="0 0 814 1000"
              fill="currentColor"
            >
              <path d="M788.1 340.9c-5.8 4.5-108.2 62.2-108.2 190.5 0 148.4 130.3 200.9 134.2 202.2-.6 3.2-20.7 71.9-68.7 141.9-42.8 61.6-87.5 123.1-155.5 123.1s-85.5-39.5-164-39.5c-76.5 0-103.7 40.8-165.9 40.8s-105.6-57.8-155.5-127.4c-58.3-81.5-105.4-208.3-105.4-328 0-193.2 125.6-295.6 249.2-295.6 65.7 0 120.5 43.1 161.7 43.1 39.2 0 100.4-45.8 175.1-45.8 28.2 0 130 2.6 197 99.7zm-234.1-187.4c31.3-36.9 53.4-88.1 53.4-139.3 0-7.1-.7-14.3-1.3-20.1-51 1.9-110.7 33.9-147 75.8-28.9 32.6-57.1 84.5-57.1 136.5 0 7.8.7 15.6 1.3 18.2 2.6.6 6.4 1.3 10.3 1.3 45.8-.1 102.5-30.4 140.4-72.4z" />
            </svg>
            <div className="hero-install-mac-text">
              <span className="hero-install-mac-label">Download for macOS</span>
              <span className="hero-install-mac-detail">Apple Silicon &middot; Same app as terminal install</span>
            </div>
          </a>
        </div>

        {/* ── Secondary links ── */}
        <div className="hero-install-extras">
          <span className="hero-install-extra">
            On macOS, both install choices end at <code>Longhouse.app</code>.
          </span>
          <span className="hero-install-extra">
            Or: <code>uv tool install longhouse</code>
          </span>
          {config.demoMode && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                trackAcquisitionEvent("demo_open_click", {
                  surface: "landing",
                  placement: "hero",
                });
                navigate("/timeline");
              }}
            >
              Try Live Demo
            </Button>
          )}
        </div>
      </div>

      {/* ── Hero device showcase ── */}
      <div className="landing-hero-devices">
        <img
          src="/images/landing/device-laptop.webp?v=4"
          alt="Longhouse timeline on a MacBook"
          className="landing-hero-device landing-hero-device--laptop"
          width={1400}
          height={871}
          loading="eager"
          fetchPriority="high"
          decoding="async"
        />
        <img
          src="/images/landing/device-iphone.webp?v=5"
          alt="Longhouse iOS widget on iPhone"
          className="landing-hero-device landing-hero-device--iphone"
          width={1024}
          height={1526}
          loading="eager"
          fetchPriority="high"
          decoding="async"
        />
        <div className="landing-hero-glow" aria-hidden="true" />
      </div>
    </section>
  );
}
