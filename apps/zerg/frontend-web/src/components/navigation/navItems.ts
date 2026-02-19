import config from "../../lib/config";

export type NavItem = {
  label: string;
  href: string;
  testId: string;
};

const BASE_ITEMS: NavItem[] = [
  { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
  { label: "Chat", href: "/chat", testId: "global-chat-tab" },
  { label: "Dashboard", href: "/dashboard", testId: "global-dashboard-tab" },
  { label: "Jobs", href: "/jobs", testId: "global-jobs-tab" },
  { label: "Forum", href: "/forum", testId: "global-forum-tab" },
  { label: "Proposals", href: "/proposals", testId: "global-proposals-tab" },
  { label: "Runners", href: "/runners", testId: "global-runners-tab" },
  { label: "Settings", href: "/settings/integrations", testId: "global-settings-tab" },
];

const DEMO_ITEMS: NavItem[] = [
  { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
];

export function getNavItems(role?: string | null): NavItem[] {
  if (config.demoMode) return [...DEMO_ITEMS];
  const items = [...BASE_ITEMS];
  if (role === "ADMIN") {
    items.push({ label: "Admin", href: "/admin", testId: "global-admin-tab" });
  }
  return items;
}
