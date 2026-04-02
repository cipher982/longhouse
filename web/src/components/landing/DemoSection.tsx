import { ProductShowcase } from "./ProductShowcase";

interface DemoSectionProps {
  screenshotTheme: "warm" | "cool-pop";
}

export function DemoSection({ screenshotTheme }: DemoSectionProps) {
  return (
    <section className="landing-demo">
      <div className="landing-section-inner">
        <span className="landing-section-label">Integrated Human View</span>
        <h2 className="landing-section-title">The UI stays. It just stops carrying the whole pitch.</h2>
        <p className="landing-section-subtitle">
          Keep the timeline, search, and session detail views. Just present them as the bundled human view
          on top of the kernel instead of as the only way to understand the product.
        </p>

        <ProductShowcase screenshotTheme={screenshotTheme} />
      </div>
    </section>
  );
}
