import config from "@/shared/lib/config";

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

export function getNavItems(): NavItem[] {
  if (config.demoMode) return [...DEMO_ITEMS];
  const items = [...BASE_ITEMS];
  if (config.singleTenant) {
    items.push({ label: "Health", href: "/health", testId: "global-health-tab" });
  }
  return items;
}
