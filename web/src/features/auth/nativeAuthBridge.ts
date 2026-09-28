import { sanitizeReturnTo } from "./loginRedirect";

type NativeAuthPayload = {
  return_to: string;
};

declare global {
  interface Window {
    LonghouseNativeAuth?: {
      requestAuth: (payload: NativeAuthPayload) => void;
    };
  }
}

export function supportsNativeAuthBridge(): boolean {
  return typeof window !== "undefined" &&
    typeof window.LonghouseNativeAuth?.requestAuth === "function";
}

export function requestNativeAuth(returnTo: string | null | undefined): boolean {
  if (!supportsNativeAuthBridge()) {
    return false;
  }

  window.LonghouseNativeAuth!.requestAuth({
    return_to: sanitizeReturnTo(returnTo),
  });
  return true;
}
