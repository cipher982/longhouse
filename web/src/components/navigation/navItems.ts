import config from "../../lib/config";

export type NavItem = {
  label: string;
  href: string;
  testId: string;
};

const BASE_ITEMS: NavItem[] = [
  { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
  { label: "Machines", href: "/runners", testId: "global-runners-tab" },
];

const DEMO_ITEMS: NavItem[] = [
  { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
];

export function getNavItems(role?: string | null): NavItem[] {
  if (config.demoMode) return [...DEMO_ITEMS];
  const items = [...BASE_ITEMS];
  if (config.singleTenant) {
    items.push({ label: "Health", href: "/health", testId: "global-health-tab" });
  }
  if (role === "ADMIN") {
    items.push({ label: "Admin", href: "/admin", testId: "global-admin-tab" });
  }
  return items;
}
