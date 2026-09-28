import { useState } from "react";

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

export function AppScreenshotFrame({ title, className = "", ...shot }: AppScreenshotFrameProps) {
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
      <div className={`app-screenshot-content${shot.mobileSrc ? " has-mobile" : ""}`}>
        {/* Keyed by src so each image starts with fresh load state. */}
        <Screenshot key={shot.src} {...shot} />
      </div>
    </div>
  );
}

function Screenshot({
  src,
  mobileSrc,
  alt,
  loading = "lazy",
  fetchPriority = "low",
}: Omit<AppScreenshotFrameProps, "title" | "className">) {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);

  return (
    <>
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
          // A cached image can finish loading before React attaches onLoad,
          // which left it invisible behind the skeleton on reload.
          ref={(img) => {
            if (img?.complete && img.naturalWidth > 0) setLoaded(true);
          }}
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
    </>
  );
}
