/**
 * SocialProof
 *
 * Credibility section: "Built by" info, open-source badge, video walkthrough placeholder.
 * Sits near the bottom of the landing page, before the footer CTA.
 *
 * To add a video later, set the `videoUrl` prop (YouTube/Loom embed URL)
 * or configure VITE_WALKTHROUGH_VIDEO_URL in the environment.
 */

interface SocialProofProps {
  /** YouTube or Loom embed URL. When set, renders an iframe instead of placeholder. */
  videoUrl?: string;
}

const GITHUB_PROFILE = "https://github.com/cipher982";
const GITHUB_REPO = "https://github.com/cipher982/longhouse";
const TWITTER_PROFILE = "https://x.com/drose_999";
const LICENSE = "Apache-2.0";

export function SocialProof({ videoUrl }: SocialProofProps) {
  const resolvedVideoUrl =
    videoUrl || import.meta.env?.VITE_WALKTHROUGH_VIDEO_URL || undefined;

  return (
    <section className="landing-social-proof" id="about">
      <div className="landing-section-inner">
        {/* Builder credibility */}
        <div className="social-proof-builder">
          <p className="social-proof-built-by">
            Built by{" "}
            <a
              href={GITHUB_PROFILE}
              target="_blank"
              rel="noopener noreferrer"
              className="social-proof-link"
            >
              David Rose
            </a>
          </p>

          <div className="social-proof-links">
            <a
              href={GITHUB_PROFILE}
              target="_blank"
              rel="noopener noreferrer"
              className="social-proof-badge"
              aria-label="GitHub profile"
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
              </svg>
              <span>GitHub</span>
            </a>

            <a
              href={TWITTER_PROFILE}
              target="_blank"
              rel="noopener noreferrer"
              className="social-proof-badge"
              aria-label="X / Twitter profile"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
              </svg>
              <span>@drose_999</span>
            </a>

            <a
              href={GITHUB_REPO}
              target="_blank"
              rel="noopener noreferrer"
              className="social-proof-badge"
              aria-label="Source code repository"
            >
              <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                <path fillRule="evenodd" d="M8.75.75a.75.75 0 00-1.5 0V2h-.984c-.305 0-.604.08-.869.23l-1.288.737A.25.25 0 013.984 3H1.75a.75.75 0 000 1.5h.428L.066 9.192a.75.75 0 00.154.838l.53-.53-.53.53v.001l.002.002.004.004.012.011a1.745 1.745 0 00.208.171c.14.104.345.233.626.355C1.464 10.8 2.08 11 2.92 11c.84 0 1.456-.2 1.848-.426.281-.122.486-.251.626-.355a1.745 1.745 0 00.208-.171l.012-.011.004-.004.002-.002L5.62 10l.53.53a.75.75 0 00.154-.838L4.182 4.5h.302a.25.25 0 01.124.033l1.29.736c.264.152.563.231.868.231h.984V13h-2.5a.75.75 0 000 1.5h6.5a.75.75 0 000-1.5h-2.5V5.5h.984c.305 0 .604-.08.869-.23l1.289-.737a.25.25 0 01.124-.033h.302L10.18 9.192a.75.75 0 00.154.838l.53-.53-.53.53v.001l.002.002.004.004.012.011a1.745 1.745 0 00.208.171c.14.104.345.233.626.355.393.226 1.008.426 1.849.426.84 0 1.456-.2 1.848-.426.281-.122.486-.251.626-.355a1.745 1.745 0 00.208-.171l.012-.011.004-.004.002-.002.001-.001L16 10l-.53.53a.75.75 0 00.154-.838L13.56 4.5h.428a.75.75 0 000-1.5h-2.234a.25.25 0 01-.124-.033l-1.29-.736A1.75 1.75 0 009.47 2H8.75V.75zM2.92 9.5c-.413 0-.733-.1-.92-.183l.92-2.893.92 2.893c-.187.083-.507.183-.92.183zm10.16 0c-.413 0-.733-.1-.92-.183l.92-2.893.92 2.893c-.187.083-.507.183-.92.183z" />
              </svg>
              <span>{LICENSE}</span>
            </a>
          </div>
        </div>

        {/* Video walkthrough */}
        <div className="social-proof-video">
          {resolvedVideoUrl ? (
            <div className="social-proof-video-frame">
              <iframe
                src={resolvedVideoUrl}
                title="Longhouse walkthrough"
                allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                allowFullScreen
                className="social-proof-video-iframe"
              />
            </div>
          ) : (
            <div className="social-proof-video-frame social-proof-video-placeholder">
              <div className="social-proof-video-play">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <path d="M8 5v14l11-7z" />
                </svg>
              </div>
              <p className="social-proof-video-label">Video walkthrough coming soon</p>
              <p className="social-proof-video-sub">Install, timeline, search -- in 60 seconds</p>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
