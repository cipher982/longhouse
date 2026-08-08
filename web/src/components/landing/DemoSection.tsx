import { ProductShowcase } from "./ProductShowcase";

interface DemoSectionProps {
  screenshotTheme: "warm" | "cool-pop";
}

export function DemoSection({ screenshotTheme }: DemoSectionProps) {
  return (
    <section className="landing-demo" id="sessions">
      <div className="landing-section-inner">
        <div className="landing-section-heading landing-section-heading--split">
          <h2>All of your session history, together.</h2>
          <p>
            Longhouse imports existing sessions from every connected machine. Search by the
            words you remember, inspect the full transcript and tool calls, and return to the
            exact context you need.
          </p>
        </div>

        <ProductShowcase screenshotTheme={screenshotTheme} />
      </div>
    </section>
  );
}
