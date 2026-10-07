/**
 * `/timeline/new`: the new-session composer as the main pane beside the
 * session rail. `?machine=<device id>` preselects a machine (the Machines
 * page's "New session").
 */
import { useCallback } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { SessionRailFrame } from "@/features/session/rail/SessionRail";
import NewSessionComposer from "./NewSessionComposer";

export default function NewSessionPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const machine = params.get("machine") ?? undefined;
  const onLaunched = useCallback(
    (sessionId: string) => navigate(`/timeline/${sessionId}`, { state: { from: "/timeline" } }),
    [navigate],
  );
  return (
    <SessionRailFrame activeSessionId={null} returnTo="/timeline">
      <NewSessionComposer key={machine ?? ""} initialDeviceId={machine} onLaunched={onLaunched} />
    </SessionRailFrame>
  );
}
