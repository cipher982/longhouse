import type { ReactNode } from "react";
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { vi, beforeAll, afterAll, afterEach } from "vitest";

function createMemoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear() {
      values.clear();
    },
    getItem(key: string) {
      return values.get(String(key)) ?? null;
    },
    key(index: number) {
      return Array.from(values.keys())[index] ?? null;
    },
    removeItem(key: string) {
      values.delete(String(key));
    },
    setItem(key: string, value: string) {
      values.set(String(key), String(value));
    },
  };
}

// Node's built-in storage globals are disabled without --localstorage-file;
// keep the jsdom tests isolated and file-free with an in-memory fallback.
for (const name of ["localStorage", "sessionStorage"] as const) {
  if (typeof window[name] === "undefined") {
    Object.defineProperty(window, name, {
      configurable: true,
      value: createMemoryStorage(),
    });
  }
}
type MemoryIndexedDb = {
  stores: Map<string, Map<IDBValidKey, unknown>>;
  database: {
    createObjectStore: (storeName: string) => IDBObjectStore;
    transaction: (storeName: string, mode: IDBTransactionMode) => {
      objectStore: (name: string) => {
        put: (value: unknown, key: IDBValidKey) => IDBRequest<unknown>;
        get: (key: IDBValidKey) => IDBRequest<unknown>;
        delete: (key: IDBValidKey) => IDBRequest<undefined>;
      };
    };
  };
};

const memoryIndexedDbs = new Map<string, MemoryIndexedDb>();

function createMemoryIndexedDb(name: string): MemoryIndexedDb {
  const stores = new Map<string, Map<IDBValidKey, unknown>>();
  const database = {
    createObjectStore(storeName: string) {
      stores.set(storeName, new Map<IDBValidKey, unknown>());
      return {} as IDBObjectStore;
    },
    transaction(storeName: string, _mode: IDBTransactionMode) {
      const store = stores.get(storeName) ?? new Map<IDBValidKey, unknown>();
      stores.set(storeName, store);
      return {
        objectStore(_name: string) {
          return {
            put(value: unknown, key: IDBValidKey) {
              return createMemoryRequest(() => {
                store.set(key, value);
                return value;
              });
            },
            get(key: IDBValidKey) {
              return createMemoryRequest(() => store.get(key));
            },
            delete(key: IDBValidKey) {
              return createMemoryRequest(() => {
                store.delete(key);
                return undefined;
              });
            },
          };
        },
      };
    },
  };
  const result = { stores, database };
  memoryIndexedDbs.set(name, result);
  return result;
}

function createMemoryRequest<T>(operation: () => T): IDBRequest<T> {
  const request = {} as IDBRequest<T>;
  queueMicrotask(() => {
    try {
      Object.assign(request, { result: operation() });
      request.onsuccess?.(new Event("success") as Event);
    } catch (error) {
      Object.assign(request, { error });
      request.onerror?.(new Event("error") as Event);
    }
  });
  return request;
}

if (typeof indexedDB === "undefined") {
  Object.defineProperty(window, "indexedDB", {
    configurable: true,
    value: {
      open(name: string) {
        const request = {} as IDBOpenDBRequest;
        queueMicrotask(() => {
          const existing = memoryIndexedDbs.get(name);
          const memoryDb = existing ?? createMemoryIndexedDb(name);
          Object.assign(request, { result: memoryDb.database });
          if (!existing) {
            request.onupgradeneeded?.(
              new Event("upgradeneeded") as IDBVersionChangeEvent,
            );
          }
          request.onsuccess?.(new Event("success") as Event);
        });
        return request;
      },
    } as IDBFactory,
  });
}

// Suppress expected test output
// These are warnings/errors that are deliberately triggered by tests verifying edge case handling
const originalWarn = console.warn;
const originalError = console.error;

beforeAll(() => {
  console.warn = (...args: unknown[]) => {
    const msg = String(args[0] || '');
    // Progress orphan/edge case warnings - tests deliberately trigger these
    if (msg.includes('[SessionProgress]')) return;
    originalWarn.apply(console, args);
  };

  console.error = (...args: unknown[]) => {
    const msg = String(args[0] || '');
    // API errors from tests that verify error handling (e.g., IntegrationsPage 500 test)
    if (msg.includes('[API]') && msg.includes('failed with status')) return;
    originalError.apply(console, args);
  };
});

afterAll(() => {
  console.warn = originalWarn;
  console.error = originalError;
});

afterEach(() => {
  cleanup();
  for (const memoryDb of memoryIndexedDbs.values()) {
    for (const store of memoryDb.stores.values()) {
      store.clear();
    }
  }
});

vi.mock("../lib/auth", () => {
  const noop = async () => {};
  return {
    useAuth: () => ({
      user: {
        id: 1,
        email: "test@local",
        display_name: "Test User",
        is_active: true,
        created_at: new Date(0).toISOString(),
      },
      isAuthenticated: true,
      isLoading: false,
      login: noop,
      logout: noop,
    }),
    AuthProvider: ({ children }: { children: ReactNode }) => children,
  };
});

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  onopen: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(public url: string) {}

  send() {}

  close() {
    if (this.onclose) {
      this.onclose(new Event('close') as CloseEvent);
    }
  }

  addEventListener(_type: string, _listener: EventListener) {}
  removeEventListener(_type: string, _listener: EventListener) {}
}

// @ts-expect-error – jsdom lacks WebSocket; provide lightweight shim for tests
global.WebSocket = MockWebSocket;

// Mock react-router hooks for components that use navigation
vi.mock("react-router", async () => {
  const actual = await vi.importActual("react-router");
  return {
    ...actual,
    useNavigate: () => vi.fn(),
  };
});
