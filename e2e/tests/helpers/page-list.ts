/**
 * Shared page definitions for visual baseline and comparison tests.
 * Single source of truth — used by ui_baseline_app, ui_baseline_public,
 * and visual_compare specs.
 */

export const BASE_QUERY = "clock=frozen&effects=off&seed=ui-baseline";

export interface PageDef {
  name: string;
  path: string;
  ready: "page" | "settings" | "domcontent";
}

export const APP_PAGES: PageDef[] = [
  { name: "timeline", path: `/timeline?${BASE_QUERY}`, ready: "page" },
  { name: "machines", path: `/runners?${BASE_QUERY}`, ready: "page" },
  { name: "health", path: `/health?${BASE_QUERY}`, ready: "page" },
  { name: "settings", path: `/settings?${BASE_QUERY}`, ready: "settings" },
  { name: "profile", path: `/profile?${BASE_QUERY}`, ready: "page" },
  { name: "integrations", path: `/settings/integrations?${BASE_QUERY}`, ready: "page" },
  { name: "devices", path: `/settings/devices?${BASE_QUERY}`, ready: "page" },
  { name: "admin", path: `/admin?${BASE_QUERY}`, ready: "page" },
];

export const PUBLIC_PAGES: PageDef[] = [
  { name: "landing", path: `/landing?${BASE_QUERY}&fx=none&video=poster`, ready: "domcontent" },
  { name: "pricing", path: `/pricing?${BASE_QUERY}`, ready: "domcontent" },
  { name: "docs", path: `/docs?${BASE_QUERY}`, ready: "domcontent" },
  { name: "docs-quickstart", path: `/docs/quickstart?${BASE_QUERY}`, ready: "domcontent" },
  { name: "docs-search", path: `/docs/search?${BASE_QUERY}`, ready: "domcontent" },
  { name: "docs-remote-control", path: `/docs/remote-control?${BASE_QUERY}`, ready: "domcontent" },
  { name: "docs-cli", path: `/docs/cli?${BASE_QUERY}`, ready: "domcontent" },
  { name: "docs-api", path: `/docs/api?${BASE_QUERY}`, ready: "domcontent" },
  { name: "docs-integrations", path: `/docs/integrations?${BASE_QUERY}`, ready: "domcontent" },
  { name: "docs-configuration", path: `/docs/configuration?${BASE_QUERY}`, ready: "domcontent" },
  { name: "changelog", path: `/changelog?${BASE_QUERY}`, ready: "domcontent" },
  { name: "privacy", path: `/privacy?${BASE_QUERY}`, ready: "domcontent" },
  { name: "security", path: `/security?${BASE_QUERY}`, ready: "domcontent" },
];
