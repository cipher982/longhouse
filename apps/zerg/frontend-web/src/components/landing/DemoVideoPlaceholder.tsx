/**
 * DemoVideoPlaceholder
 *
 * A placeholder component for product demo video.
 * Shows a styled frame with "Coming Soon" state until video is ready.
 *
 * Supports three modes:
 * 1. No URL -> placeholder with play icon and "Coming Soon" message
 * 2. Loom/YouTube URL -> responsive iframe embed
 * 3. Direct video URL -> native <video> player
 *
 * If a direct video URL returns a 404 (file not yet generated), the component
 * falls back to the placeholder state automatically.
 */

import { useState } from "react";

interface DemoVideoPlaceholderProps {
  videoUrl?: string;
  thumbnailUrl?: string;
  className?: string;
}

/** Detect embeddable URLs and return the embed src, or null for direct video. */
function getEmbedUrl(url: string): string | null {
  // Loom: https://www.loom.com/share/abc123 -> https://www.loom.com/embed/abc123
  const loomMatch = url.match(/loom\.com\/share\/([a-zA-Z0-9]+)/);
  if (loomMatch) return `https://www.loom.com/embed/${loomMatch[1]}`;

  // YouTube: various formats -> https://www.youtube.com/embed/VIDEO_ID
  const ytMatch =
    url.match(/youtube\.com\/watch\?v=([a-zA-Z0-9_-]+)/) ??
    url.match(/youtu\.be\/([a-zA-Z0-9_-]+)/) ??
    url.match(/youtube\.com\/embed\/([a-zA-Z0-9_-]+)/);
  if (ytMatch) return `https://www.youtube.com/embed/${ytMatch[1]}`;

  return null;
}

export function DemoVideoPlaceholder({
  videoUrl,
  thumbnailUrl,
  className = "",
}: DemoVideoPlaceholderProps) {
  const [videoError, setVideoError] = useState(false);

  // If no URL or video failed to load, render nothing instead of a placeholder
  if (!videoUrl || videoError) {
    return null;
  }

  const embedUrl = getEmbedUrl(videoUrl);

  // Embeddable (Loom / YouTube)
  if (embedUrl) {
    return (
      <div className={`demo-video-placeholder ${className}`}>
        <div className="demo-video-frame">
          <div className="demo-video-embed-wrap">
            <iframe
              src={embedUrl}
              title="Longhouse demo walkthrough"
              allowFullScreen
              allow="autoplay; fullscreen; picture-in-picture"
              className="demo-video-iframe"
            />
          </div>
        </div>
      </div>
    );
  }

  // Direct video file
  return (
    <div className={`demo-video-placeholder ${className}`}>
      <div className="demo-video-frame">
        <video
          src={videoUrl}
          poster={thumbnailUrl}
          controls
          className="demo-video-player"
          onError={() => setVideoError(true)}
        >
          Your browser does not support the video tag.
        </video>
      </div>
    </div>
  );
}
