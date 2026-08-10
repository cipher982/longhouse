/**
 * Machine-surface beat. Distinct shape: a real terminal on the left, copy on
 * the right (mirrors the thesis section's copy-left/visual-right to alternate
 * the page rhythm). No kicker chip, no serif punchline, no card grid.
 */

// Every command here must exist in the real CLI (`longhouse sessions --help`).
// A visitor who copy-pastes from a marketing terminal and hits "command not
// found" costs more trust than the section buys.
const TERMINAL_LINES: { prompt?: boolean; text: string; tone?: "dim" | "gold" }[] = [
  { prompt: true, text: "longhouse claude" },
  { text: "control attached · opening Claude Code", tone: "gold" },
  { prompt: true, text: "longhouse sessions get 3f2a" },
  { text: "macbook   repair OAuth refresh    Claude   live", tone: "gold" },
  { prompt: true, text: "longhouse sessions events 3f2a" },
  { text: "running tests: 14 passed, 0 failed", tone: "dim" },
  { prompt: true, text: 'longhouse sessions continue 3f2a "open the PR"' },
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
            The agent runs on your machine, with your accounts.
          </h2>
          <p className="landing-surface-lead">
            Longhouse drives the Claude Code, Codex, Cursor, and OpenCode binaries you
            already installed. Your credentials, your files, your MCP servers, and your
            provider config stay exactly where they are, and the terminal stays usable
            at your desk. Longhouse carries your instructions to it and records what
            happens.
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
