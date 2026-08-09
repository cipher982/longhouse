interface PhoneSessionTranscript {
  assistantLine: string;
  sentMessage?: string;
}

export interface PhoneSessionScreenProps {
  title: string;
  transcript: PhoneSessionTranscript;
  composerText: string;
  sent: boolean;
  working: boolean;
  onSend: () => void;
}

function SignalGlyphs() {
  return (
    <span className="phone-session-system-icons" aria-hidden="true">
      <svg className="phone-session-signal" viewBox="0 0 18 15" fill="currentColor">
        <rect x="0" y="10" width="3" height="5" rx="1" />
        <rect x="5" y="7" width="3" height="8" rx="1" />
        <rect x="10" y="3" width="3" height="12" rx="1" />
        <rect x="15" y="0" width="3" height="15" rx="1" />
      </svg>
      <svg className="phone-session-wifi" viewBox="0 0 20 15" fill="none">
        <path d="M1.5 4.5C6.5 0.5 13.5 0.5 18.5 4.5" />
        <path d="M4.5 8C7.7 5.5 12.3 5.5 15.5 8" />
        <path d="M8 11.5C9.2 10.6 10.8 10.6 12 11.5" />
        <circle cx="10" cy="14" r="1" fill="currentColor" stroke="none" />
      </svg>
      <svg className="phone-session-battery" viewBox="0 0 27 14" fill="none">
        <rect x="0.75" y="0.75" width="23" height="12.5" rx="3.5" />
        <path d="M26 5v4" />
        <rect x="3" y="3" width="18" height="8" rx="2" fill="currentColor" stroke="none" />
      </svg>
    </span>
  );
}

function WandGlyph() {
  return (
    <svg className="phone-session-wand" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="m5 15 9.5-9.5" />
      <path d="m4 4 12 12" />
      <path d="M15.5 1.5v3M14 3h3M3 13.5v3M1.5 15h3" />
    </svg>
  );
}

function BellGlyph() {
  return (
    <svg className="phone-session-bell" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M5 17h14l-1.5-2.5V10a5.5 5.5 0 0 0-11 0v4.5L5 17Z" />
      <path d="M9.5 20h5" />
    </svg>
  );
}

function BackGlyph() {
  return (
    <svg className="phone-session-back" viewBox="0 0 14 24" fill="none" aria-hidden="true">
      <path d="m10.5 3-7 9 7 9" />
    </svg>
  );
}

export function PhoneSessionScreen({
  title,
  transcript,
  composerText,
  sent,
  working,
  onSend,
}: PhoneSessionScreenProps) {
  const canSend = composerText.trim().length > 0 && !sent;

  return (
    <div className="phone-session-screen">
      <div className="phone-session-transcript" aria-live="polite">
        <div className="phone-session-message phone-session-message-assistant">
          {transcript.assistantLine}
        </div>
        {sent && transcript.sentMessage ? (
          <div className={`phone-session-submitted${working ? " working" : ""}`}>
            <div className="phone-session-message phone-session-message-user">
              <span className="phone-session-bubble">{transcript.sentMessage}</span>
            </div>
            <div className="phone-session-origin">Longhouse</div>
            {working ? <div className="submitted-status">Working…</div> : null}
          </div>
        ) : null}
      </div>

      <div className="phone-session-statusbar">
        <span className="phone-session-time">9:41</span>
        <SignalGlyphs />
      </div>

      <nav className="phone-session-nav" aria-label="Session navigation">
        <button type="button" className="phone-session-back-button" aria-label="Back">
          <BackGlyph />
        </button>
        <span className="phone-session-title">{title}</span>
        <div className="phone-session-nav-trailing">
          <span className="phone-session-assist">
            <WandGlyph />
            <span>Assist</span>
          </span>
          <button type="button" className="phone-session-bell-button" aria-label="Notifications">
            <BellGlyph />
          </button>
        </div>
      </nav>

      <section className="phone-session-bottom-card" aria-label="Session controls">
        <div className="phone-session-runtime">
          <div className={`phone-session-state${working ? " is-working" : ""}`}>
            <span className="phone-session-state-indicator" aria-hidden="true" />
            <strong>{working ? "Working" : "Idle"}</strong>
            {!working ? (
              <>
                <span className="phone-session-state-separator">·</span>
                <span className="phone-session-state-detail">Waiting for input</span>
              </>
            ) : null}
          </div>
          <span className="phone-session-capability">
            <span className="phone-session-capability-dot" aria-hidden="true" />
            macbook
          </span>
        </div>

        <div className="phone-session-composer">
          <button type="button" className="phone-session-plus" aria-label="Add attachment">
            +
          </button>
          <input
            className="phone-session-input"
            type="text"
            value={composerText}
            placeholder="Send a message to the live session…"
            readOnly
            aria-label="Message to live session"
          />
          <button
            type="button"
            className={`phone-session-send${canSend ? " is-armed" : ""}`}
            disabled={!canSend}
            onClick={onSend}
            aria-label={sent ? "Message sent" : "Send"}
          >
            ↑
          </button>
        </div>
      </section>
    </div>
  );
}
