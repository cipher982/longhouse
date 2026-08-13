import { RemoteScenePlayer } from "../components/remote-scene/RemoteScenePlayer";
import { usePageMeta } from "../hooks/usePageMeta";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import "../styles/remote-scene-prototype.css";

export default function RemoteScenePrototypePage() {
  usePublicPageScroll();
  usePageMeta({
    title: "Longhouse remote scene prototype",
    description: "A visual prototype of an awake workstation and a coding session reachable from a phone.",
  });

  return (
    <div className="remote-scene-page">
      <header className="remote-scene-header">
        <a href="/" className="remote-scene-wordmark">Longhouse</a>
        <span>visual prototype / isolated review</span>
      </header>

      <main className="remote-scene-main">
        <section className="remote-scene-intro" aria-labelledby="remote-scene-title">
          <p className="remote-scene-kicker">A six-second study in continuity</p>
          <h1 id="remote-scene-title">Leave the room.<br /><em>Keep the session.</em></h1>
          <p className="remote-scene-lead">
            The workstation stays awake while you step away. The same coding session remains
            reachable from your phone, ready for the next instruction.
          </p>
        </section>

        <RemoteScenePlayer />

        <section className="remote-scene-notes" aria-label="Prototype notes">
          <div>
            <span className="remote-scene-note-index">01</span>
            <strong>Work happens on the workstation.</strong>
            <p>This scene keeps the active terminal attached to the awake studio-mac.</p>
          </div>
          <div>
            <span className="remote-scene-note-index">02</span>
            <strong>Reachability shifts to the phone.</strong>
            <p>The phone is a control surface, not a second execution machine.</p>
          </div>
          <div>
            <span className="remote-scene-note-index">03</span>
            <strong>Static is the fallback.</strong>
            <p>Use Static frame or the system reduced-motion preference for a still composition.</p>
          </div>
        </section>

        <p className="remote-scene-footnote">
          Deterministic scene with a recorded real Claude Code PTY. No live provider session,
          audio, or production landing integration is connected.
        </p>
      </main>
    </div>
  );
}
