import { useEffect, useMemo, useRef, useState, type ChangeEvent, type MouseEvent } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ProviderGlyph } from "../../components/ProviderGlyph";
import { TimelinePane } from "../../components/session-workspace/TimelinePane";
import { getProviderLabel } from "../../lib/providers";
import { getSessionCardText } from "../../lib/sessionUtils";
import { buildTimelineModel } from "../../lib/sessionWorkspace";
import { LiveWorkRibbon } from "./LiveWorkRibbon";
import { buildReplayFrame, parseCapture, REPLAY_DURATION_MS, SCENES } from "./model";
import type { Scene, SessionCapture } from "./types";
import "../../styles/tokens.css";
import "../../styles/session-workspace.css";
import "./App.css";

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

function formatReplayTime(timeMs: number): string {
  const seconds = Math.floor(timeMs / 1000);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function preventPreviewNavigation(event: MouseEvent<HTMLElement>) {
  if (event.target instanceof Element && event.target.closest("a")) {
    event.preventDefault();
    event.stopPropagation();
  }
}

export default function App() {
  const [capture, setCapture] = useState<SessionCapture | null>(null);
  const [captureName, setCaptureName] = useState("");
  const [fileError, setFileError] = useState<string | null>(null);
  const [loadingFile, setLoadingFile] = useState(false);
  const [scene, setScene] = useState<Scene>("recorded");
  const [timeMs, setTimeMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [motionOverride, setMotionOverride] = useState<boolean | null>(null);
  const [largerText, setLargerText] = useState(false);
  const [draft, setDraft] = useState("");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [showAbandonedBranches, setShowAbandonedBranches] = useState(false);
  const [controlsOpen, setControlsOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const timeRef = useRef(0);
  const prefersReducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  const compact = useMediaQuery("(max-width: 600px)");
  const reduceMotion = motionOverride ?? prefersReducedMotion;

  const frame = useMemo(
    () => capture ? buildReplayFrame(capture, scene, timeMs) : null,
    [capture, scene, timeMs],
  );
  const frameItems = frame?.items;
  const timeline = useMemo(
    () => frameItems ? buildTimelineModel(frameItems) : null,
    [frameItems],
  );
  const loadedEntries = useMemo(
    () => frameItems?.filter((item) => item.kind !== "seam").length ?? 0,
    [frameItems],
  );
  const omittedEntries = useMemo(() => {
    if (!capture) return 0;
    const capturedEntries = capture.workspace.projection.items.filter((item) => item.kind !== "seam").length;
    return Math.max(0, capture.workspace.projection.total - capturedEntries);
  }, [capture]);
  const session = capture?.workspace.session;
  const sessionTitle = session ? getSessionCardText(session, { titleMaxChars: 160 }).title : "";
  const activeScene = SCENES.find((candidate) => candidate.id === scene);
  const provenance = scene === "recorded"
    ? "Recorded snapshot · no live connection"
    : "Design replay · real recorded content · simulated states";

  function seek(nextTimeMs: number) {
    const next = Math.max(0, Math.min(REPLAY_DURATION_MS, nextTimeMs));
    timeRef.current = next;
    setTimeMs(next);
  }

  useEffect(() => {
    if (!playing) return;
    const startedAt = performance.now();
    const startedAtMs = timeRef.current;
    let animationFrame = 0;
    const tick = (now: number) => {
      const elapsed = Math.min(REPLAY_DURATION_MS, startedAtMs + now - startedAt);
      const next = elapsed >= REPLAY_DURATION_MS ? REPLAY_DURATION_MS : Math.max(startedAtMs, Math.floor(elapsed / 50) * 50);
      if (next !== timeRef.current) {
        timeRef.current = next;
        setTimeMs(next);
      }
      if (elapsed >= REPLAY_DURATION_MS) {
        setPlaying(false);
      } else {
        animationFrame = requestAnimationFrame(tick);
      }
    };
    animationFrame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(animationFrame);
  }, [playing]);

  async function loadCapture(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    if (!file) return;
    setLoadingFile(true);
    setFileError(null);
    setPlaying(false);
    try {
      const nextCapture = parseCapture(await file.text());
      setCapture(nextCapture);
      setCaptureName(file.name);
      setScene("recorded");
      seek(0);
      setDraft("");
      setSelectedKey(null);
      setShowAbandonedBranches(false);
    } catch {
      setFileError("Could not read this capture. Choose a Longhouse live-status capture JSON file.");
    } finally {
      setLoadingFile(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  function changeScene(nextScene: Scene) {
    setPlaying(false);
    seek(0);
    setScene(nextScene);
  }

  function togglePlayback() {
    if (playing) {
      setPlaying(false);
      return;
    }
    if (timeRef.current >= REPLAY_DURATION_MS) seek(0);
    setPlaying(true);
  }

  const chooseFile = () => fileInputRef.current?.click();

  return (
    <div
      className="lab-app"
      data-testid={capture ? "lab-ready" : undefined}
      data-scene={scene}
      data-time-ms={timeMs}
      data-theme={theme}
      data-reduce-motion={reduceMotion}
      data-larger-text={largerText}
    >
      <header className="lab-toolbar">
        <div className="lab-toolbar__identity">
          <strong>Longhouse</strong>
          <span className="lab-toolbar__divider" aria-hidden="true" />
          <span>Design lab</span>
        </div>
        <span className="lab-toolbar__scope">Local only</span>
      </header>

      <input
        ref={fileInputRef}
        id="capture-file"
        className="lab-file-input"
        type="file"
        accept="application/json,.json"
        aria-label="Load session capture"
        disabled={loadingFile}
        onChange={(event) => void loadCapture(event)}
      />

      <div className="lab-studio">
        <aside className="lab-controls" aria-label="Replay controls">
          <details
            className="lab-controls__details"
            data-testid="lab-controls-disclosure"
            open={!compact || controlsOpen}
            onToggle={(event) => {
              if (compact) setControlsOpen(event.currentTarget.open);
            }}
          >
            <summary className="lab-controls__summary">
              <span>Replay controls</span>
              <span className="lab-controls__chevron" aria-hidden="true">⌄</span>
            </summary>
            <div className="lab-controls__body">
              <h2 className="lab-controls__heading">Replay controls</h2>
              <section className="lab-control-section">
                <span className="lab-control-label">Source</span>
                <p className="lab-capture-name" title={captureName || undefined}>
                  {captureName || "No capture loaded"}
                </p>
                {capture ? <p className="lab-control-note">Recorded {new Date(capture.capturedAt).toLocaleString()}</p> : null}
                <button className="lab-button" type="button" onClick={chooseFile} disabled={loadingFile}>
                  {loadingFile ? "Reading capture…" : capture ? "Replace capture" : "Choose capture"}
                </button>
                {fileError ? <p className="lab-file-error" role="alert">{fileError}</p> : null}
              </section>

              <section className="lab-control-section">
                <label className="lab-control-label" htmlFor="lab-scenario">Scenario</label>
                <select
                  id="lab-scenario"
                  aria-label="Scenario"
                  value={scene}
                  disabled={!capture}
                  onChange={(event) => changeScene(event.target.value as Scene)}
                >
                  {SCENES.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
                </select>
                <p className="lab-control-note">{activeScene?.description}</p>
                <div className="lab-playback">
                  <button
                    className="lab-button lab-button--play"
                    type="button"
                    aria-label={playing ? "Pause replay" : "Play replay"}
                    disabled={!capture || scene === "recorded" || loadingFile}
                    onClick={togglePlayback}
                  >
                    <span aria-hidden="true">{playing ? "Ⅱ" : "▷"}</span>
                    {playing ? "Pause" : "Play"}
                  </button>
                  <output className="lab-time" htmlFor="lab-time-range">{formatReplayTime(timeMs)} / {formatReplayTime(REPLAY_DURATION_MS)}</output>
                </div>
                <input
                  id="lab-time-range"
                  aria-label="Replay time"
                  aria-valuetext={`${formatReplayTime(timeMs)} of ${formatReplayTime(REPLAY_DURATION_MS)}`}
                  type="range"
                  min={0}
                  max={REPLAY_DURATION_MS}
                  step={1}
                  value={timeMs}
                  disabled={!capture || scene === "recorded"}
                  onChange={(event) => {
                    setPlaying(false);
                    seek(Number(event.target.value));
                  }}
                />
              </section>

              <section className="lab-control-section">
                <label className="lab-control-label" htmlFor="lab-theme">Appearance</label>
                <select id="lab-theme" aria-label="Theme" value={theme} onChange={(event) => setTheme(event.target.value as "dark" | "light")}>
                  <option value="dark">Graphite · dark</option>
                  <option value="light">Paper · light</option>
                </select>
                <label className="lab-check">
                  <input type="checkbox" checked={reduceMotion} onChange={(event) => setMotionOverride(event.target.checked)} />
                  <span>Reduce live motion</span>
                </label>
                <label className="lab-check">
                  <input type="checkbox" checked={largerText} onChange={(event) => setLargerText(event.target.checked)} />
                  <span>Larger text</span>
                </label>
              </section>

              <p className="lab-controls__footnote">
                Capture and draft stay in this tab’s memory. Nothing is uploaded or sent. Transcript links and external media are disabled.
              </p>
              <p className="lab-controls__footnote lab-compact-note">The phone-sized view is a compact web design study, not a native iOS feature.</p>
            </div>
          </details>
        </aside>

        <main className="lab-stage">
          <div className="lab-stage__caption">
            <span>{capture ? provenance : "Live confidence · a design study"}</span>
            <span className="lab-stage__surface-label">Session workspace</span>
          </div>
          {capture && frame && timeline && session ? (
            <section
              className="lab-preview"
              aria-label="Session design preview"
              onClickCapture={preventPreviewNavigation}
              onAuxClickCapture={preventPreviewNavigation}
            >
              <TimelinePane
                key={captureName + capture.capturedAt}
                items={timeline.items}
                loadedEntries={loadedEntries}
                totalEntries={loadedEntries + omittedEntries}
                abandonedEvents={0}
                showAbandonedBranches={showAbandonedBranches}
                onShowAbandonedBranchesChange={setShowAbandonedBranches}
                hasPreviousPage={false}
                isFetchingPreviousPage={false}
                onFetchPreviousPage={() => undefined}
                selectedKey={selectedKey}
                onSelectKey={setSelectedKey}
                renderMedia={false}
                headerLeft={
                  <div className="lab-session-heading">
                    <h1 title={sessionTitle}>{sessionTitle}</h1>
                    <span className="lab-session-provider">
                      <ProviderGlyph provider={session.provider} size={15} variant="bare" tone="mono" />
                      {getProviderLabel(session.provider)}
                    </span>
                  </div>
                }
                dock={
                  <div className="lab-composer">
                    <LiveWorkRibbon state={frame.ribbon} motionTimeMs={timeMs} reduceMotion={reduceMotion} />
                    <div className="lab-composer__draft">
                      <label className="lab-visually-hidden" htmlFor="lab-draft">Draft message</label>
                      <textarea
                        id="lab-draft"
                        rows={2}
                        placeholder="Write the next instruction…"
                        value={draft}
                        onChange={(event) => setDraft(event.target.value)}
                        aria-describedby="lab-draft-helper"
                      />
                      <div className="lab-composer__footer">
                        <span id="lab-draft-helper">Preview only · draft stays here</span>
                        <button type="button" className="lab-queue" disabled title="Design preview; never sends to the session">Queue <span aria-hidden="true">↑</span></button>
                      </div>
                    </div>
                  </div>
                }
              />
            </section>
          ) : (
            <section className="lab-empty" aria-labelledby="lab-empty-title">
              <div className="lab-empty__content">
                <p className="lab-empty__eyebrow">Real content. Considered confidence.</p>
                <h1 id="lab-empty-title">Start with a real session.</h1>
                <p className="lab-empty__intro">Open a recorded Longhouse capture to explore how work, silence, and connection changes should feel.</p>
                <button className="lab-button lab-button--primary" type="button" onClick={chooseFile} disabled={loadingFile}>
                  {loadingFile ? "Reading capture…" : "Open capture JSON"}
                  <span aria-hidden="true">↗</span>
                </button>
                {fileError ? <p className="lab-file-error" role="alert">{fileError}</p> : null}
                <div className="lab-empty__steps">
                  <div><span>01</span><p>Choose a local capture.<small>The original conversation stays intact.</small></p></div>
                  <div><span>02</span><p>Explore a simulated state.<small>Seek, pause, and compare the quiet details.</small></p></div>
                  <div><span>03</span><p>Keep the real session untouched.<small>No live connection. No commands sent.</small></p></div>
                </div>
                <p className="lab-empty__privacy">Read locally, kept only in memory. Reload to clear.</p>
              </div>
            </section>
          )}
        </main>
      </div>
    </div>
  );
}

const root = document.getElementById("live-status-lab-root");
if (root) createRoot(root).render(<BrowserRouter><App /></BrowserRouter>);
