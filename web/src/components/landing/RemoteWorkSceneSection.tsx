import { RemoteScenePlayer } from "../remote-scene/RemoteScenePlayer";
import "../../styles/remote-work-scene.css";

export function RemoteWorkSceneSection() {
  return (
    <section id="continuous-work" className="landing-remote-scene">
      <div className="landing-section-inner">
        <div className="landing-section-heading landing-section-heading--split">
          <div>
            <p className="landing-remote-scene-kicker">KEEP WORKING</p>
            <h2>Leave the room.<br />Keep the session.</h2>
          </div>
          <p>
            Sessions launched through Longhouse keep running in the provider CLI on
            your machine. Watch the same work from your browser or iPhone, then send
            the next instruction without returning to your desk.
          </p>
        </div>

        <div className="landing-remote-scene-player">
          <RemoteScenePlayer />
        </div>
        <p className="landing-remote-scene-note">
          The opening uses a real recorded Claude Code session. Continuing tasks are
          simulated locally so the scene can keep working without a live model.
        </p>
      </div>
    </section>
  );
}
