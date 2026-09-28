// The one channel from the document to the app: WebKit's `longhouse` script
// message handler. Native validates every message (a UUID for openSubagent).

type NativeMessage = { type: "openSubagent"; sessionId: string };

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
