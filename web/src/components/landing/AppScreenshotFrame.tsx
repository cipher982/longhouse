import { useEffect, useState } from "react";

interface AppScreenshotFrameProps {
  src: string;
  /** Capture of the app's own phone layout, used below 640px wide. */
  mobileSrc?: string;
  alt: string;
  title?: string;
  className?: string;
  loading?: "eager" | "lazy";
  fetchPriority?: "high" | "low" | "auto";
}

export function AppScreenshotFrame({
  src,
  mobileSrc,
  alt,
  title,
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
    <div className={`app-screenshot-frame ${className}`}>
      <div className="app-screenshot-chrome">
        <div className="app-screenshot-dots">
          <span className="dot dot-red" />
          <span className="dot dot-yellow" />
          <span className="dot dot-green" />
        </div>
        {title && <div className="app-screenshot-title">{title}</div>}
      </div>
      <div className={`app-screenshot-content${mobileSrc ? " has-mobile" : ""}`}>
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
        <picture>
          {mobileSrc ? <source media="(max-width: 640px)" srcSet={mobileSrc} /> : null}
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
        </picture>
      </div>
    </div>
  );
}
