import type { ReactNode } from "react";
import { SessionLedger } from "../../components/session-workspace/SessionLedger";
import type { LiveWorkRibbonProps } from "./types";
import "../../components/session-workspace/SessionLedger.css";
import "./LiveWorkRibbon.css";

/**
 * Replay-only adapter. The production SessionLedger owns hierarchy, evidence
 * language, motion gates, and disclosure; the lab supplies only its local
 * preview choice slot and recorded state.
 */
export function LiveWorkRibbon({
  state,
  motionTimeMs,
  reduceMotion,
  surface,
  notice,
  previewDecision,
  onPreviewDecision,
}: LiveWorkRibbonProps) {
  let actionSlot: ReactNode = null;
  if (state.tone === "attention" && surface !== "dock") {
    actionSlot = (
      <div className="lwr-approval">
        <div className="lwr-approval__actions">
          <button
            type="button"
            disabled={previewDecision !== null}
            onClick={() => onPreviewDecision("deny")}
          >
            Deny
          </button>
          <button
            type="button"
            disabled={previewDecision !== null}
            onClick={() => onPreviewDecision("allow")}
          >
            Allow once
          </button>
        </div>
        <p className="lwr-approval__note" role="status">
          {previewDecision
            ? `Mock choice: ${previewDecision === "allow" ? "Allow once" : "Deny"}. No command sent; request remains pending.`
            : "Preview choices only · no command is sent"}
        </p>
      </div>
    );
  }
  return (
    <SessionLedger
      state={state}
      motionTimeMs={motionTimeMs}
      reduceMotion={reduceMotion}
      surface={surface}
      notice={notice}
      actionSlot={actionSlot}
      testId="live-work-ribbon"
    />
  );
}
