import { useEffect, useState } from "react";

interface AppScreenshotFrameProps {
  src: string;
  alt: string;
  /** When set, renders an autoplaying looped clip with `src` as its poster. */
  videoSrc?: string;
  title?: string;
  aspectRatio?: "16/9" | "4/3" | "21/9";
  showChrome?: boolean;
  theme?: "warm" | "cool-pop";
  className?: string;
  loading?: "eager" | "lazy";
  fetchPriority?: "high" | "low" | "auto";
}

export function AppScreenshotFrame({
  src,
  alt,
  videoSrc,
  title,
  aspectRatio = "16/9",
  showChrome = true,
  theme = "warm",
  className = "",
  loading = "lazy",
  fetchPriority = "low",
}: AppScreenshotFrameProps) {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    setLoaded(false);
    setError(false);
  }, [src]);

  return (
    <div className={`app-screenshot-frame app-screenshot-frame--${theme} ${className}`}>
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
            <p>Screenshot unavailable</p>
          </div>
        )}
        {videoSrc ? (
          <video
            src={videoSrc}
            poster={src}
            aria-label={alt}
            autoPlay
            muted
            loop
            playsInline
            preload="metadata"
            onLoadedData={() => setLoaded(true)}
            onError={() => setError(true)}
            style={{ opacity: loaded ? 1 : 0 }}
          />
        ) : (
          <img
            src={src}
            alt={alt}
            onLoad={() => setLoaded(true)}
            onError={() => setError(true)}
            loading={loading}
            decoding="async"
            fetchPriority={fetchPriority}
            style={{ opacity: loaded ? 1 : 0 }}
          />
        )}
      </div>
    </div>
  );
}
