/**
 * Machine-surface beat. Distinct shape: a real terminal on the left, copy on
 * the right (mirrors the thesis section's copy-left/visual-right to alternate
 * the page rhythm). No kicker chip, no serif punchline, no card grid.
 */

const TERMINAL_LINES: { prompt?: boolean; text: string; tone?: "dim" | "gold" }[] = [
  { prompt: true, text: "longhouse wall" },
  { text: "longhouse   refactor auth module     Claude    live", tone: "gold" },
  { text: "api-gw      fix flaky upload test     Codex     2m ago", tone: "dim" },
  { text: "infra       rotate staging secrets    Antigr.   8m ago", tone: "dim" },
  { prompt: true, text: "longhouse tail 3f2a" },
  { text: "→ running tests: 14 passed, 0 failed", tone: "dim" },
  { prompt: true, text: 'longhouse continue 3f2a "open the PR"' },
  { text: "↳ steering session on cinder…", tone: "gold" },
];

export function MachineSurfaceSection() {
  return (
    <section className="landing-surface" id="surface">
      <div className="landing-section-inner landing-surface-inner">
        <div className="landing-surface-terminal" aria-hidden="true">
          <div className="landing-surface-terminal-bar">
            <span className="dot" />
            <span className="dot" />
            <span className="dot" />
          </div>
          <pre className="landing-surface-terminal-body">
            {TERMINAL_LINES.map((line, i) => (
              <div key={i} className={`tline ${line.tone ?? ""}`}>
                {line.prompt ? <span className="tprompt">$ </span> : null}
                {line.text}
              </div>
            ))}
          </pre>
        </div>

        <div className="landing-surface-copy">
          <h2 className="landing-surface-title">
            Everything the browser does, your scripts can too.
          </h2>
          <p className="landing-surface-lead">
            <code>wall</code>, <code>tail</code>, <code>continue</code>, and{" "}
            <code>/api/agents/*</code> are first-class. The browser is just the bundled
            workspace over the same session model — not a separate source of truth.
          </p>
          <p className="landing-surface-links">
            <a href="/docs/cli">CLI reference</a>
            <span aria-hidden="true"> · </span>
            <a href="/docs/api">Machine API</a>
          </p>
        </div>
      </div>
    </section>
  );
}
