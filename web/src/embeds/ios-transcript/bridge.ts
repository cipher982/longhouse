// The one channel from the document to the app: WebKit's `longhouse` script
// message handler. Native validates every message (a UUID for openSubagent,
// a non-empty request id for submitted-input actions, cursors it rendered for
// loadToolBodies).

type NativeMessage =
  | { type: "openSubagent"; sessionId: string }
  | { type: "editSubmitted" | "discardSubmitted" | "retrySubmitted"; clientRequestId: string }
  | { type: "loadToolBodies"; cursors: string[] };

interface WebKitBridgeWindow {
  webkit?: { messageHandlers?: { longhouse?: { postMessage(message: NativeMessage): void } } };
}

export function postToNative(message: NativeMessage): void {
  try {
    (window as unknown as WebKitBridgeWindow).webkit!.messageHandlers!.longhouse!.postMessage(message);
  } catch {
    /* The bridge is absent in previews and tests; the row stays inert. */
  }
}
