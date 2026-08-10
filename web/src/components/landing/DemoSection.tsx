import { ProductShowcase } from "./ProductShowcase";

interface DemoSectionProps {
  screenshotTheme: "warm" | "cool-pop";
}

export function DemoSection({ screenshotTheme }: DemoSectionProps) {
  return (
    <section className="landing-demo" id="sessions">
      <div className="landing-section-inner">
        <div className="landing-section-heading landing-section-heading--split">
          <h2>Every session, on every machine, in one place.</h2>
          <p>
            Claude Code, Codex, Cursor, and OpenCode each write their sessions to their
            own files, in their own formats, on whichever machine ran them. Longhouse
            reads all of it and gives you one list you can open, search, and act on.
            When a session is under Longhouse control, you send the next instruction
            straight from the row.
          </p>
        </div>

        <ProductShowcase screenshotTheme={screenshotTheme} />
      </div>
    </section>
  );
}
