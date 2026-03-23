// Type definitions for Longhouse funnel tracking

interface LonghouseFunnelAPI {
  track: (eventType: string, metadata?: Record<string, any>) => void;
  getVisitorId: () => string;
  flush: () => void;
}

declare global {
  interface Window {
    LonghouseFunnel?: LonghouseFunnelAPI;
  }
}

export {};
