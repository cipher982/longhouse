/**
 * DemoSection
 *
 * Product demo section with video placeholder.
 * Can be updated with actual video URL when ready.
 */

import { DemoVideoPlaceholder } from "./DemoVideoPlaceholder";

export function DemoSection() {
  return (
    <section className="landing-demo">
      <div className="landing-section-inner">
        <span className="landing-section-label">See it in action</span>
        <h2 className="landing-section-title">Watch How It Works</h2>
        <p className="landing-section-subtitle">
          A quick walkthrough of building your first AI workflow
        </p>

        <DemoVideoPlaceholder
          // videoUrl="/videos/swarmlet-demo.mp4"  // Uncomment when video is ready
          // thumbnailUrl="/images/landing/demo-thumbnail.jpg"
        />
      </div>
    </section>
  );
}
