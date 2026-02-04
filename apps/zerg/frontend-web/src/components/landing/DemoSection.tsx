/**
 * DemoSection
 *
 * Product showcase section with tabbed screenshots.
 * Shows Timeline and Session Detail views.
 */

import { ProductShowcase } from "./ProductShowcase";

export function DemoSection() {
  return (
    <section className="landing-demo">
      <div className="landing-section-inner">
        <span className="landing-section-label">See it in action</span>
        <h2 className="landing-section-title">Your AI Session Archive</h2>
        <p className="landing-section-subtitle">
          Searchable timeline of every Claude Code session. Resume any conversation from any device.
        </p>

        <ProductShowcase />
      </div>
    </section>
  );
}
