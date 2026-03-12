import config from "../../lib/config";

export type NavItem = {
  label: string;
  href: string;
  testId: string;
};

const BASE_ITEMS: NavItem[] = [
  { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
  { label: "Inbox", href: "/conversations", testId: "global-inbox-tab" },
  { label: "Oikos", href: "/chat", testId: "global-chat-tab" },
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
