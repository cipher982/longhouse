import type { HeroSession, TranscriptItem } from "@longhouse/video/demo";

/**
 * The Longhouse iOS session screen, drawn in the browser: nav with session
 * title, the mirrored transcript, and the bottom steering card. Layout
 * follows the real app (see `make sim-shot`); the transcript is the same
 * scripted turns the recorded terminal printed.
 */
export function HeroPhone({
  narrow,
  width,
  session,
  project,
  history,
  prompt,
  typedChars,
  sentAgo,
  reply,
  working,
}: {
  narrow: boolean;
  width: number;
  session: HeroSession;
  project: string;
  history: readonly TranscriptItem[];
  prompt: string;
  typedChars: number;
  /** Seconds since Send (negative before). */
  sentAgo: number;
  reply: readonly TranscriptItem[];
  working: boolean;
}) {
  const sent = sentAgo >= 0;
  const typed = sent ? "" : prompt.slice(0, Math.max(0, typedChars));
  // Type is set a little larger than true phone scale so it reads at hero size.
  const pt = narrow ? width / 330 : width / 250;
  const pressed = sentAgo >= -0.12 && sentAgo < 0.18;

  return (
    <div
      className={`hero-phone${narrow ? " is-narrow" : ""}`}
      style={{ ["--pt" as string]: `${pt.toFixed(4)}px` }}
      aria-hidden="true"
    >
      <div className="hero-phone-screen">
        {narrow ? null : (
          <div className="hero-phone-status">
            <span>9:41</span>
            <span className="hero-phone-island" />
            <span className="hero-phone-battery" />
          </div>
        )}
        <div className="hero-phone-nav">
          <span className="hero-phone-circle">‹</span>
          <span className="hero-phone-heading">
            <strong>{session.title}</strong>
            <span>
              Claude · {project} · {session.machine}
            </span>
          </span>
          <span className="hero-phone-circle">···</span>
        </div>

        <div className="hero-phone-transcript">
          {history.map((item, i) => (
            <TranscriptRow item={item} key={`h${i}`} />
          ))}
          {sent ? (
            <div className="hero-phone-user">
              <span>{prompt}</span>
            </div>
          ) : null}
          {reply.map((item, i) => (
            <TranscriptRow item={item} key={`r${i}`} />
          ))}
        </div>

        <div className="hero-phone-sheet">
          <div className={`hero-phone-state${working ? " is-working" : ""}`}>
            <i aria-hidden="true" />
            <strong>{working ? "Working" : "Idle"}</strong>
            <span>{working ? `on ${session.machine}` : "Waiting for input"}</span>
          </div>
          <div className="hero-phone-composer">
            <span className="hero-phone-plus">+</span>
            <span className={`hero-phone-input${typed ? " has-text" : ""}`}>
              {typed || "Steer this turn"}
              {typed && !sent ? <i className="hero-phone-caret" /> : null}
            </span>
            <span
              className={`hero-phone-send${typed ? " is-armed" : ""}${pressed ? " is-pressed" : ""}`}
            >
              ↑
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function TranscriptRow({ item }: { item: TranscriptItem }) {
  return item.kind === "tool" ? (
    <div className="hero-phone-tool">{item.text}</div>
  ) : (
    <p className="hero-phone-msg">{item.text}</p>
  );
}
