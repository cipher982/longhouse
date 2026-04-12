import { ProductShowcase } from "./ProductShowcase";

interface DemoSectionProps {
  screenshotTheme: "warm" | "cool-pop";
}

export function DemoSection({ screenshotTheme }: DemoSectionProps) {
  return (
    <section className="landing-demo">
      <div className="landing-section-inner">
        <span className="landing-section-label">The Product</span>
        <h2 className="landing-section-title">Timeline. Search. Session detail.</h2>
        <p className="landing-section-subtitle">
          One UI for the whole session archive. Search across providers, inspect raw transcripts,
          and pick up where any session left off.
        </p>

        <ProductShowcase screenshotTheme={screenshotTheme} />
      </div>
    </section>
  );
}
