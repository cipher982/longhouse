import type { HeroSession } from "@longhouse/video/demo";
import { ProviderGlyph } from "../../ProviderGlyph";
import type { HeroLayout } from "./heroLayout";

/**
 * The docked sessions, drawn in the web timeline's row grammar (glyph, title,
 * latest reply, state, mode chip, machine, recency) so the page shows the
 * product's shape rather than a marketing table. Titles and previews are the
 * recordings' own prompts and final replies.
 */
export function HeroTimeline({
  layout,
  sessions,
  opacity,
  rowsIn,
  focus,
}: {
  layout: HeroLayout;
  sessions: HeroSession[];
  opacity: number;
  rowsIn: number[];
  /** 0→1: the Claude row lifts as the handoff begins. */
  focus: number;
}) {
  if (opacity <= 0) return null;
  const { panel } = layout;
  return (
    <div
      className="hero-stage-item hero-timeline"
      style={{
        width: panel.w,
        zIndex: 20,
        opacity,
        transform: `translate(${panel.x}px, ${panel.y}px)`,
      }}
    >
      <div className="hero-timeline-header" style={{ height: layout.panelHeaderH }}>
        {layout.narrow ? null : (
          <div className="hero-timeline-toolbar" aria-hidden="true">
            <span className="hero-timeline-search">Search sessions</span>
            <span className="hero-timeline-tasks">{sessions.length} tasks</span>
            <span className="hero-timeline-start">Start a session</span>
          </div>
        )}
        <div className="hero-timeline-section">
        <span className="hero-timeline-heading">Live now</span>
        <span className="hero-timeline-count">{sessions.length}</span>
        <span className="hero-timeline-rule" aria-hidden="true" />
        </div>
      </div>
      {sessions.map((session, i) => {
        const rowIn = rowsIn[i] ?? 1;
        const lifted = i === 0 ? focus : 0;
        return (
          <div
            key={session.id}
            className="hero-timeline-row"
            style={{
              height: layout.rowH,
              ["--row-lift" as string]: lifted.toFixed(3),
            }}
          >
            <span className="hero-timeline-slot" style={{ opacity: rowIn }}>
              <ProviderGlyph provider={session.glyph} size={layout.narrow ? 20 : 22} />
            </span>
            <span className="hero-timeline-main" style={{ opacity: rowIn }}>
              <span className="hero-timeline-title">{session.title}</span>
              <span className="hero-timeline-preview">{session.preview}</span>
            </span>
            <span className="hero-timeline-meta" style={{ opacity: rowIn }}>
              <span className="hero-timeline-state">idle</span>
              <span className={`hero-timeline-mode is-${session.mode.toLowerCase()}`}>
                {session.mode}
              </span>
              <span className="hero-timeline-machine">on {session.machine}</span>
              <span className="hero-timeline-ago">{session.ago}</span>
            </span>
          </div>
        );
      })}
    </div>
  );
}
