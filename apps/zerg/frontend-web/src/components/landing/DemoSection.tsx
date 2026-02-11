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
const DEMO_VIDEO_URL: string | undefined = "/videos/timeline-demo.mp4";

export function DemoSection() {
  return (
    <section className="landing-demo">
      <div className="landing-section-inner">
        <span className="landing-section-label">See it in action</span>
        <h2 className="landing-section-title">Your AI Session Archive</h2>
        <p className="landing-section-subtitle">
          Searchable timeline of every Claude Code session. Hosted keeps your archive available everywhere.
        </p>

        <ProductShowcase />

        <DemoVideoPlaceholder videoUrl={DEMO_VIDEO_URL} />
      </div>
    </section>
  );
}
