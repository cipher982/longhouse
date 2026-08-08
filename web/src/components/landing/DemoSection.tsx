import { ProductShowcase } from "./ProductShowcase";

interface DemoSectionProps {
  screenshotTheme: "warm" | "cool-pop";
}

export function DemoSection({ screenshotTheme }: DemoSectionProps) {
  return (
    <section className="landing-demo" id="sessions">
      <div className="landing-section-inner">
        <div className="landing-section-heading landing-section-heading--split">
          <h2>Four agents&rsquo; output, one normalized system.</h2>
          <p>
            Claude Code, Codex, Cursor, and OpenCode each write their own logs, in their
            own formats, on whichever machine ran them. Longhouse ingests all of it into
            one schema — transcripts, tool calls, timing, state — so one timeline answers
            what any agent did on any machine.
          </p>
        </div>

        <ProductShowcase screenshotTheme={screenshotTheme} />
      </div>
    </section>
  );
}
