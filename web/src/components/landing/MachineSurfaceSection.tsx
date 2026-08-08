/**
 * Machine-surface beat. Distinct shape: a real terminal on the left, copy on
 * the right (mirrors the thesis section's copy-left/visual-right to alternate
 * the page rhythm). No kicker chip, no serif punchline, no card grid.
 */

const TERMINAL_LINES: { prompt?: boolean; text: string; tone?: "dim" | "gold" }[] = [
  { prompt: true, text: "longhouse claude" },
  { text: "control attached · opening Claude Code", tone: "gold" },
  { prompt: true, text: "longhouse wall" },
  { text: "macbook   repair OAuth refresh    Claude   live", tone: "gold" },
  { text: "devbox    fix release build       Codex    2m ago", tone: "dim" },
  { prompt: true, text: "longhouse tail 3f2a" },
  { text: "running tests: 14 passed, 0 failed", tone: "dim" },
  { prompt: true, text: 'longhouse send 3f2a "open the PR"' },
];

export function MachineSurfaceSection() {
  return (
    <section className="landing-surface" id="execution">
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
            Your installed agents still do the work.
          </h2>
          <p className="landing-surface-lead">
            Longhouse uses the Claude, Codex, Cursor, and OpenCode clients already on
            your machines. Their accounts, local files, tools, and configuration stay
            in place. Longhouse records the sessions and exposes the controls.
          </p>
          <p className="landing-surface-links">
            <a href="/docs/cli">CLI reference</a>
            <a href="/docs/api">Machine API</a>
          </p>
        </div>
      </div>
    </section>
  );
}
