import { ProviderGlyph } from "../ProviderGlyph";

/**
 * Shows the product boundary directly: Helm has a two-way control path while
 * Shadow streams an externally launched session into Longhouse for observation.
 * The surrounding section carries the same meaning in accessible copy, so this
 * compact animated diagram stays out of the accessibility tree.
 */
export function ThesisControlFlow() {
  return (
    <div className="thesis-control-flow" aria-hidden="true">
      <div className="thesis-control-head">
        <span className="thesis-mode-chip thesis-mode-chip--helm">Helm</span>
        <span className="thesis-control-kicker">two-way control</span>
        <span className="thesis-control-live">
          <span className="thesis-control-live-dot" /> Live
        </span>
      </div>

      <div className="thesis-control-stage">
        <div className="thesis-control-track" />
        <div className="thesis-control-packet thesis-control-packet--command">
          run tests
        </div>
        <div className="thesis-control-packet thesis-control-packet--result">
          84 passed
        </div>

        <div className="thesis-control-device thesis-control-terminal">
          <div className="thesis-control-device-head">
            <span className="thesis-window-dots"><i /><i /><i /></span>
            <ProviderGlyph provider="claude" size={13} variant="bare" />
            <strong>Claude Code</strong>
            <span>macbook</span>
          </div>
          <div className="thesis-control-terminal-body">
            <p><span className="thesis-terminal-prompt">›</span> Ship when tests pass</p>
            <p className="thesis-terminal-tool"><span>●</span> Bash(python3 -m pytest)</p>
            <p className="thesis-terminal-result">84 passed in 3.1s</p>
          </div>
        </div>

        <div className="thesis-control-hub">
          <span className="thesis-control-hub-mark">L</span>
          <small>Longhouse</small>
        </div>

        <div className="thesis-control-device thesis-control-phone">
          <div className="thesis-control-phone-bar">
            <span>9:41</span>
            <i />
          </div>
          <div className="thesis-control-phone-title">Claude Code</div>
          <div className="thesis-control-phone-message">Ship when tests pass</div>
          <div className="thesis-control-phone-event">
            <span>●</span>
            <div><strong>Ran tests</strong><small>84 passed in 3.1s</small></div>
          </div>
          <div className="thesis-control-phone-composer">
            <span>Send next instruction</span><b>↑</b>
          </div>
        </div>
      </div>

      <div className="thesis-shadow-flow">
        <div className="thesis-shadow-meta">
          <span className="thesis-mode-chip">Shadow</span>
          <span>started outside Longhouse</span>
        </div>
        <div className="thesis-shadow-session">
          <ProviderGlyph provider="codex" size={14} variant="bare" />
          <strong>Codex CLI</strong>
          <span className="thesis-shadow-machine">devbox</span>
        </div>
        <div className="thesis-shadow-route">
          <span className="thesis-shadow-route-line"><i /></span>
          <span className="thesis-shadow-route-label">events stream in</span>
          <b>Watch + search</b>
        </div>
      </div>
    </div>
  );
}
