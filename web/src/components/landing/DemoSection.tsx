import { ProductShowcase } from "./ProductShowcase";

interface DemoSectionProps {
  screenshotTheme: "warm" | "cool-pop";
}

export function DemoSection({ screenshotTheme }: DemoSectionProps) {
  return (
    <section className="landing-demo">
      <div className="landing-section-inner">
        <span className="landing-section-label">Integrated Human View</span>
        <h2 className="landing-section-title">The UI is where the control proof becomes visible.</h2>
        <p className="landing-section-subtitle">
          Keep the timeline, search, and session detail views. Just present them as the bundled human view
          over the session surface you can search, message, and keep working from other interfaces.
        </p>

        <ProductShowcase screenshotTheme={screenshotTheme} />
      </div>
    </section>
  );
}
