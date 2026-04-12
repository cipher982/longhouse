import { useState } from "react";
import { AppScreenshotFrame } from "./AppScreenshotFrame";

const installCommand = "curl -fsSL https://get.longhouse.ai/install.sh | bash";

interface InstallSectionProps {
  className?: string;
}

export function InstallSection({ className = "" }: InstallSectionProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(installCommand);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      const textArea = document.createElement("textarea");
      textArea.value = installCommand;
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand("copy");
      document.body.removeChild(textArea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <section id="landing-install" className={`install-section ${className}`}>
      <div className="landing-section-inner">
        <p className="landing-section-label">Start Here</p>
        <h2 className="landing-section-title">Start locally in one command.</h2>
        <p className="landing-section-subtitle">
          Install on the machine where you work. On macOS, <code>Longhouse.app</code> becomes the visible
          local status and repair surface, while the CLI path stays first-class for agents and power users.
        </p>

        <div className="landing-install-grid">
          <div className="landing-install-main">
            <div className="install-command-container">
              <button
                type="button"
                className="install-command"
                onClick={handleCopy}
                aria-label={`Copy install command: ${installCommand}`}
              >
                <span className="install-prompt" aria-hidden="true">$</span>
                <code className="install-text">{installCommand}</code>
              </button>
              <button
                type="button"
                className={`install-copy-btn ${copied ? "copied" : ""}`}
                onClick={handleCopy}
                aria-label={copied ? "Copied!" : "Copy to clipboard"}
              >
                {copied ? (
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                    <path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z" />
                  </svg>
                ) : (
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                    <path d="M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 010 1.5h-1.5a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-1.5a.75.75 0 011.5 0v1.5A1.75 1.75 0 019.25 16h-7.5A1.75 1.75 0 010 14.25v-7.5z" />
                    <path d="M5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0114.25 11h-7.5A1.75 1.75 0 015 9.25v-7.5zm1.75-.25a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-7.5a.25.25 0 00-.25-.25h-7.5z" />
                  </svg>
                )}
              </button>
            </div>

            <p className="install-note">macOS, Linux, or WSL. No account required to get first value.</p>

            <div className="install-features">
              <span className="install-feature">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                  <path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z" />
                </svg>
                Guided onboarding
              </span>
              <span className="install-feature">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                  <path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z" />
                </svg>
                No sudo required
              </span>
              <span className="install-feature">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                  <path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z" />
                </svg>
                <code>Longhouse.app</code> on macOS
              </span>
            </div>

            <div className="landing-install-paths">
              <article className="landing-install-path">
                <h3>Try it on your laptop</h3>
                <p>
                  The quick win is local: install, import your first real sessions, and open the timeline.
                  Great for proving the product. It stops when the laptop sleeps.
                </p>
              </article>
              <article className="landing-install-path">
                <h3>Move durability later</h3>
                <p>
                  When you want Longhouse to stay on, put the Runtime Host on a VPS, Mac mini, or homelab box
                  and point your machines at it.
                </p>
              </article>
            </div>

            <p className="landing-install-alt-path">
              Prefer packages? <code>uv tool install longhouse</code> stays first-class for agents and power users.
            </p>
          </div>

          <aside className="landing-install-proof">
            <p className="landing-install-proof-label">macOS surface</p>
            <AppScreenshotFrame
              src="/images/landing/ambient-menu-bar.png"
              alt="Longhouse menu bar app showing local machine health"
              title="Longhouse.app"
              aspectRatio="21/9"
              showChrome={false}
              className="landing-install-proof-frame"
            />
            <p className="landing-install-proof-caption">
              Quiet by default, but always a visible local status and repair path.
            </p>
          </aside>
        </div>
      </div>
    </section>
  );
}
