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
 * Replace with a Loom or YouTube URL once the recording is ready.
 * When set, the placeholder will render an actual video player.
 */
const DEMO_VIDEO_URL: string | undefined = undefined;

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
