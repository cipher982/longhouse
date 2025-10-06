declare global {
  interface Window {
    __TEST_WORKER_ID__?: string | number;
  }
}

export {};
