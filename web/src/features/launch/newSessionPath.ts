/** Where "New session" goes: the composer pane beside the rail. */
export const NEW_SESSION_PATH = "/timeline/new";

/** `?machine=` preselects that machine when it can launch. */
export function newSessionPath(deviceId?: string | null): string {
  return deviceId ? `${NEW_SESSION_PATH}?machine=${encodeURIComponent(deviceId)}` : NEW_SESSION_PATH;
}
