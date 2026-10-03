import config from "@/shared/lib/config";

export type NavItem = {
  label: string;
  href: string;
  testId: string;
};

const BASE_ITEMS: NavItem[] = [
  { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
  { label: "Machines", href: "/machines", testId: "global-machines-tab" },
];

const DEMO_ITEMS: NavItem[] = [
  { label: "Timeline", href: "/timeline", testId: "global-timeline-tab" },
];

export function getNavItems(): NavItem[] {
  return config.demoMode ? [...DEMO_ITEMS] : [...BASE_ITEMS];
}
