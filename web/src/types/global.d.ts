declare global {
  interface Window {
    __TEST_COMMIS_ID__?: string | number;
    __TEST_WORKSPACE_FALLBACK_MS__?: number;
  }
}

export {};
