import { useSyncExternalStore } from "react";
import {
  hostLinkStore,
  type HostLinkSnapshot,
  type HostLinkStore,
} from "./store";

export function useHostLink(
  store: HostLinkStore = hostLinkStore,
): HostLinkSnapshot {
  return useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
}
