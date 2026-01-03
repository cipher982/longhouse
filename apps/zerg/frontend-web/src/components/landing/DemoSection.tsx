/**
 * DemoSection
 *
 * Product showcase section with tabbed screenshots.
 * Shows Chat, Dashboard, and Canvas views.
 */

import { ProductShowcase } from "./ProductShowcase";

export function DemoSection() {
  return (
    <section className="landing-demo">
      <div className="landing-section-inner">
        <span className="landing-section-label">See it in action</span>
        <h2 className="landing-section-title">Everything You Need, One Place</h2>
        <p className="landing-section-subtitle">
          Chat with your AI, monitor your agents, and build workflows visually.
        </p>

        <ProductShowcase />
      </div>
    </section>
  );
}
