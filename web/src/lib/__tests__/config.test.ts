import { afterEach, describe, expect, it, vi } from "vitest";

function setRequiredRuntimeConfig() {
  window.API_BASE_URL = "/api";
  window.WS_BASE_URL = "wss://longhouse.ai/api/ws";
  window.__APP_MODE__ = "production";
}

async function loadConfigModule() {
  vi.resetModules();
  return import("../config");
}

afterEach(() => {
  vi.unstubAllEnvs();
  delete window.API_BASE_URL;
  delete window.WS_BASE_URL;
  delete window.__APP_MODE__;
  delete window.__UMAMI_WEBSITE_ID__;
  delete window.__UMAMI_SCRIPT_SRC__;
  delete window.__UMAMI_DOMAINS__;
});

describe("config analytics runtime overrides", () => {
  it("prefers runtime umami config over legacy Vite env", async () => {
    setRequiredRuntimeConfig();
    window.__UMAMI_WEBSITE_ID__ = "runtime-site";
    window.__UMAMI_SCRIPT_SRC__ = "https://runtime.example/script.js";
    window.__UMAMI_DOMAINS__ = "runtime.longhouse.ai";

    vi.stubEnv("VITE_UMAMI_WEBSITE_ID", "vite-site");
    vi.stubEnv("VITE_UMAMI_SCRIPT_SRC", "https://vite.example/script.js");
    vi.stubEnv("VITE_UMAMI_DOMAINS", "vite.longhouse.ai");

    const { config } = await loadConfigModule();

    expect(config.umamiWebsiteId).toBe("runtime-site");
    expect(config.umamiScriptSrc).toBe("https://runtime.example/script.js");
    expect(config.umamiDomains).toBe("runtime.longhouse.ai");
  });

  it("falls back to legacy Vite umami env when runtime config is absent", async () => {
    setRequiredRuntimeConfig();

    vi.stubEnv("VITE_UMAMI_WEBSITE_ID", "vite-site");
    vi.stubEnv("VITE_UMAMI_SCRIPT_SRC", "https://vite.example/script.js");
    vi.stubEnv("VITE_UMAMI_DOMAINS", "vite.longhouse.ai");

    const { config } = await loadConfigModule();

    expect(config.umamiWebsiteId).toBe("vite-site");
    expect(config.umamiScriptSrc).toBe("https://vite.example/script.js");
    expect(config.umamiDomains).toBe("vite.longhouse.ai");
  });

  it("preserves explicit empty runtime values instead of falling back to legacy Vite analytics config", async () => {
    setRequiredRuntimeConfig();
    window.__UMAMI_WEBSITE_ID__ = "";
    window.__UMAMI_SCRIPT_SRC__ = "";
    window.__UMAMI_DOMAINS__ = "";

    vi.stubEnv("VITE_UMAMI_WEBSITE_ID", "vite-site");
    vi.stubEnv("VITE_UMAMI_SCRIPT_SRC", "https://vite.example/script.js");
    vi.stubEnv("VITE_UMAMI_DOMAINS", "vite.longhouse.ai");

    const { config } = await loadConfigModule();

    expect(config.umamiWebsiteId).toBe("");
    expect(config.umamiScriptSrc).toBe("https://analytics.drose.io/script.js");
    expect(config.umamiDomains).toBe("");
  });
});
