import { ThesisModeCards } from "./ThesisModeCards";

export function KernelThesisSection() {
  return (
    <section id="control" className="landing-thesis">
      <div className="landing-section-inner landing-thesis-inner">
        <div className="landing-thesis-copy">
          <h2 className="landing-thesis-title">
            Full control over sessions you launch.
          </h2>
          <p className="landing-thesis-lead">
            The provider CLI keeps running on your machine with its own account, tools,
            and repository. Launch through Longhouse and it stays steerable from the
            browser or iPhone while the terminal at your desk remains usable. Sessions
            started elsewhere still stream into the same timeline for watching and search.
          </p>
        </div>

        <div className="landing-thesis-visual">
          <ThesisModeCards />
        </div>
      </div>
    </section>
  );
}
