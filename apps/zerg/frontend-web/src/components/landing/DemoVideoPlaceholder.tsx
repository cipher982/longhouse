/**
 * DemoVideoPlaceholder
 *
 * A placeholder component for product demo video.
 * Shows a styled frame with "Coming Soon" state until video is ready.
 */

interface DemoVideoPlaceholderProps {
  videoUrl?: string;
  thumbnailUrl?: string;
  className?: string;
}

export function DemoVideoPlaceholder({
  videoUrl,
  thumbnailUrl,
  className = "",
}: DemoVideoPlaceholderProps) {
  const hasVideo = Boolean(videoUrl);

  if (!hasVideo) {
    return (
      <div className={`demo-video-placeholder ${className}`}>
        <div className="demo-video-frame">
          <div className="demo-video-chrome">
            <div className="demo-video-dots">
              <span className="dot dot-red" />
              <span className="dot dot-yellow" />
              <span className="dot dot-green" />
            </div>
            <div className="demo-video-title">Product Demo</div>
          </div>
          <div className="demo-video-content">
            <div className="demo-video-icon">▶</div>
            <div className="demo-video-text">
              <h3>Demo Video Coming Soon</h3>
              <p>See Swarmlet in action</p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Future: actual video player
  return (
    <div className={`demo-video-placeholder ${className}`}>
      <div className="demo-video-frame">
        <video
          src={videoUrl}
          poster={thumbnailUrl}
          controls
          className="demo-video-player"
        >
          Your browser does not support the video tag.
        </video>
      </div>
    </div>
  );
}
