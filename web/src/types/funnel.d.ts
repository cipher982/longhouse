type FunnelMetadataValue =
  | string
  | number
  | boolean
  | null
  | FunnelMetadataValue[]
  | { [key: string]: FunnelMetadataValue };

// Type definitions for Longhouse funnel tracking

interface LonghouseFunnelAPI {
  track: (eventType: string, metadata?: Record<string, FunnelMetadataValue>) => void;
  getVisitorId: () => string;
  flush: () => void;
}

declare global {
  interface Window {
    LonghouseFunnel?: LonghouseFunnelAPI;
  }
}

export {};
