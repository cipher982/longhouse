import { ProductShowcase } from "./ProductShowcase";

interface DemoSectionProps {
  screenshotTheme: "warm" | "cool-pop";
}

export function DemoSection({ screenshotTheme }: DemoSectionProps) {
  return (
    <section className="landing-demo">
      <div className="landing-section-inner">
        <span className="landing-section-label">Bundled Browser View</span>
        <h2 className="landing-section-title">Timeline, search, and raw session detail.</h2>
        <p className="landing-section-subtitle">
          The browser is the main workspace over the same session model. Open any session,
          inspect the raw transcript, and continue from the exact context that matters.
        </p>

        <ProductShowcase screenshotTheme={screenshotTheme} />
      </div>
    </section>
  );
}
