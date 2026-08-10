import { ProviderGlyph } from "../ProviderGlyph";

/**
 * The thesis section's visual: two compact session cards that SHOW the
 * distinction the bullets claim — a session launched through Longhouse
 * (Helm: composer live, next instruction ready to send) next to one
 * started elsewhere (Shadow: streams in, watch-only). Replaces a static
 * iPhone screenshot that duplicated the live phone replica one section
 * above. Illustrative microcopy, not a recording; the bullets beside it
 * carry the real semantics, so the visual stays aria-hidden.
 */
export function ThesisModeCards() {
  return (
    <div className="thesis-mode-cards" aria-hidden="true">
      <div className="thesis-mode-card thesis-mode-card--helm">
        <div className="thesis-mode-card-head">
          <span className="thesis-mode-chip thesis-mode-chip--helm">Helm</span>
          <span className="thesis-mode-card-origin">launched through Longhouse</span>
        </div>
        <div className="thesis-mode-card-session">
          <ProviderGlyph provider="claude" size={14} variant="bare" />
          <span className="thesis-mode-card-name">Claude Code</span>
          <span className="thesis-mode-card-machine">macbook</span>
          <span className="thesis-mode-card-state thesis-mode-card-state--live">Working</span>
        </div>
        <p className="thesis-mode-card-line">
          Refactored the retry loop. Running the full suite now.
        </p>
        <div className="thesis-mode-card-composer">
          <span className="thesis-mode-card-draft">Ship it when tests pass</span>
          <span className="thesis-mode-card-send">↑</span>
        </div>
      </div>

      <div className="thesis-mode-card thesis-mode-card--shadow">
        <div className="thesis-mode-card-head">
          <span className="thesis-mode-chip">Shadow</span>
          <span className="thesis-mode-card-origin">started in a terminal</span>
        </div>
        <div className="thesis-mode-card-session">
          <ProviderGlyph provider="codex" size={14} variant="bare" />
          <span className="thesis-mode-card-name">Codex CLI</span>
          <span className="thesis-mode-card-machine">devbox</span>
          <span className="thesis-mode-card-state">Watching</span>
        </div>
        <p className="thesis-mode-card-line">
          Rebuilding the release pipeline config.
        </p>
        <div className="thesis-mode-card-composer thesis-mode-card-composer--off">
          <span className="thesis-mode-card-draft">
            Watch-only. Launch through Longhouse to steer.
          </span>
        </div>
      </div>
    </div>
  );
}
