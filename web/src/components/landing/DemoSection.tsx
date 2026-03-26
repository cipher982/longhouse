/**
 * DemoSection
 *
 * Product showcase section with tabbed screenshots and video walkthrough.
 * Shows Timeline and Session Detail views, plus a demo video placeholder.
 */

import { ProductShowcase } from "./ProductShowcase";
import { DemoVideoPlaceholder } from "./DemoVideoPlaceholder";

/**
 * Video walkthrough URL.
 * Points to the self-hosted mp4 generated via Remotion:
 *   make video-remotion-web
 * Output lands in public/videos/timeline-demo.mp4 and is served at this path.
 * If the file hasn't been generated yet, DemoVideoPlaceholder falls back to
 * a "Coming Soon" placeholder state via its onError handler.
 */
const DEMO_VIDEO_URL: string | undefined = undefined; // TODO: re-enable once video is updated
const DEMO_VIDEO_THUMBNAIL_URL = "/images/landing/timeline-preview.png";

interface DemoSectionProps {
  screenshotTheme: "warm" | "cool-pop";
}

export function DemoSection({ screenshotTheme }: DemoSectionProps) {
  return (
    <section className="landing-demo">
      <div className="landing-section-inner">
        <span className="landing-section-label">See it in action</span>
        <h2 className="landing-section-title">One Timeline. Every Agent.</h2>
        <p className="landing-section-subtitle">
          Every Claude Code, Codex, and Gemini session lands in one searchable timeline. Claude continues directly from the web today; Codex and Gemini are archive-first for now.
        </p>

        <ProductShowcase screenshotTheme={screenshotTheme} />

        <DemoVideoPlaceholder videoUrl={DEMO_VIDEO_URL} thumbnailUrl={DEMO_VIDEO_THUMBNAIL_URL} />
      </div>
    </section>
  );
}
