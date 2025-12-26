/**
 * AppScreenshotFrame
 *
 * A styled frame component that makes screenshots look like real app UI.
 * Provides proper aspect ratio, loading states, and professional presentation.
 */

import { useState } from "react";

interface AppScreenshotFrameProps {
  src: string;
  alt: string;
  title?: string;
  aspectRatio?: "16/9" | "4/3" | "21/9";
  showChrome?: boolean;
  className?: string;
}

export function AppScreenshotFrame({
  src,
  alt,
  title,
  aspectRatio = "16/9",
  showChrome = true,
  className = "",
}: AppScreenshotFrameProps) {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);

  return (
    <div className={`app-screenshot-frame ${className}`}>
      {showChrome && (
        <div className="app-screenshot-chrome">
          <div className="app-screenshot-dots">
            <span className="dot dot-red" />
            <span className="dot dot-yellow" />
            <span className="dot dot-green" />
          </div>
          {title && <div className="app-screenshot-title">{title}</div>}
        </div>
      )}
      <div
        className="app-screenshot-content"
        style={{ aspectRatio }}
      >
        {!loaded && !error && (
          <div className="app-screenshot-skeleton">
            <div className="skeleton-pulse" />
          </div>
        )}
        {error && (
          <div className="app-screenshot-error">
            <div className="error-icon">⚠️</div>
            <p>Screenshot coming soon</p>
          </div>
        )}
        <img
          src={src}
          alt={alt}
          onLoad={() => setLoaded(true)}
          onError={() => setError(true)}
          style={{ opacity: loaded ? 1 : 0 }}
        />
      </div>
    </div>
  );
}
