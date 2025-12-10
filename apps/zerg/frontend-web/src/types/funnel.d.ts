// Type definitions for Swarmlet funnel tracking

interface SwarmletFunnelAPI {
  track: (eventType: string, metadata?: Record<string, any>) => void;
  getVisitorId: () => string;
  flush: () => void;
}

declare global {
  interface Window {
    SwarmletFunnel?: SwarmletFunnelAPI;
  }
}

export {};
