import { ProductShowcase } from "./ProductShowcase";

export function DemoSection() {
  return (
    <section className="landing-demo" id="sessions">
      <div className="landing-section-inner">
        <div className="landing-section-heading landing-section-heading--split">
          <h2>Every session, on every machine, in one place.</h2>
          <p>
            Every CLI writes its sessions to its own files, in its own formats, on
            whichever machine ran them. Longhouse reads all of it and gives you one list
            you can open, search, and act on. When a session is under Longhouse control,
            you send the next instruction straight from the row.
          </p>
        </div>

        <ProductShowcase />
      </div>
    </section>
  );
}
