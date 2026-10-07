/**
 * `/timeline/new`: the new-session composer as the main pane beside the
 * session rail. `?machine=<device id>` preselects a machine (the Machines
 * page's "New session").
 */
import { useCallback } from "react";
import { createPortal } from "react-dom";
import { useLocation, useNavigate, useSearchParams } from "react-router";
import { useHeaderSlot } from "@/app/headerSlot";
import { SessionRailFrame } from "@/features/session/rail/SessionRail";
import NewSessionComposer from "./NewSessionComposer";

export default function NewSessionPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [params] = useSearchParams();
  const headerSlot = useHeaderSlot();
  const machine = params.get("machine") ?? undefined;
  // Back goes where the user came from (a filtered Timeline keeps its filters).
  const from = (location.state as { from?: unknown } | null)?.from;
  const returnTo = typeof from === "string" && from ? from : "/timeline";
  const onLaunched = useCallback(
    (sessionId: string) => navigate(`/timeline/${sessionId}`, { state: { from: returnTo } }),
    [navigate, returnTo],
  );
  return (
    <SessionRailFrame activeSessionId={null} returnTo={returnTo}>
      {headerSlot ? createPortal(<span className="new-session__bar-title">New session</span>, headerSlot) : null}
      <NewSessionComposer key={machine ?? ""} initialDeviceId={machine} onLaunched={onLaunched} />
    </SessionRailFrame>
  );
}
