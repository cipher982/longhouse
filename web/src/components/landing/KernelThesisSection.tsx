import { ThesisControlFlow } from "./ThesisControlFlow";

export function KernelThesisSection() {
  return (
    <section id="control" className="landing-thesis">
      <div className="landing-section-inner landing-thesis-inner">
        <div className="landing-thesis-copy">
          <h2 className="landing-thesis-title">
            Launch here. Steer from anywhere.
          </h2>
          <p className="landing-thesis-lead">
            Your provider CLI keeps running on your machine with its own account, tools,
            and repository. Sessions launched through Longhouse stay steerable from the
            terminal, browser, or iPhone. Sessions started elsewhere still stream into
            the same timeline for watching and search.
          </p>
        </div>

        <div className="landing-thesis-visual">
          <ThesisControlFlow />
        </div>
      </div>
    </section>
  );
}
