import { ProductShowcase } from "./ProductShowcase";

interface DemoSectionProps {
  screenshotTheme: "warm" | "cool-pop";
}

export function DemoSection({ screenshotTheme }: DemoSectionProps) {
  return (
    <section className="landing-demo">
      <div className="landing-section-inner">
        <h2 className="landing-demo-heading">Find any past session in seconds.</h2>
        <p className="landing-demo-subhead">
          One timeline across every machine and provider. Search it, open the raw transcript,
          and pick up exactly where it left off.
        </p>

        <ProductShowcase screenshotTheme={screenshotTheme} />
      </div>
    </section>
  );
}
