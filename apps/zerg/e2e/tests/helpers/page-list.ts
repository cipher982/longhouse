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
  { name: "dashboard", path: `/dashboard?${BASE_QUERY}`, ready: "page" },
  { name: "chat", path: `/chat?${BASE_QUERY}`, ready: "page" },
  { name: "settings", path: `/settings?${BASE_QUERY}`, ready: "settings" },
  { name: "profile", path: `/profile?${BASE_QUERY}`, ready: "page" },
  { name: "runners", path: `/runners?${BASE_QUERY}`, ready: "page" },
  { name: "integrations", path: `/settings/integrations?${BASE_QUERY}`, ready: "page" },
  { name: "knowledge", path: `/settings/knowledge?${BASE_QUERY}`, ready: "page" },
  { name: "contacts", path: `/settings/contacts?${BASE_QUERY}`, ready: "page" },
  { name: "admin", path: `/admin?${BASE_QUERY}`, ready: "page" },
  { name: "traces", path: `/traces?${BASE_QUERY}`, ready: "page" },
  { name: "reliability", path: `/reliability?${BASE_QUERY}`, ready: "page" },
];

export const PUBLIC_PAGES: PageDef[] = [
  { name: "landing", path: `/landing?${BASE_QUERY}&fx=none`, ready: "domcontent" },
  { name: "pricing", path: `/pricing?${BASE_QUERY}`, ready: "domcontent" },
  { name: "docs", path: `/docs?${BASE_QUERY}`, ready: "domcontent" },
  { name: "changelog", path: `/changelog?${BASE_QUERY}`, ready: "domcontent" },
  { name: "privacy", path: `/privacy?${BASE_QUERY}`, ready: "domcontent" },
  { name: "security", path: `/security?${BASE_QUERY}`, ready: "domcontent" },
];
