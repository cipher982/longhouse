export type NavItem = {
  label: string;
  href: string;
  testId: string;
};

const BASE_ITEMS: NavItem[] = [
  { label: "Chat", href: "/chat", testId: "global-chat-tab" },
  { label: "Dashboard", href: "/dashboard", testId: "global-dashboard-tab" },
  { label: "Swarm", href: "/swarm", testId: "global-swarm-tab" },
  { label: "Canvas", href: "/canvas", testId: "global-canvas-tab" },
  { label: "Integrations", href: "/settings/integrations", testId: "global-integrations-tab" },
  { label: "Contacts", href: "/settings/contacts", testId: "global-contacts-tab" },
  { label: "Runners", href: "/runners", testId: "global-runners-tab" },
];

export function getNavItems(role?: string | null): NavItem[] {
  const items = [...BASE_ITEMS];
  if (role === "ADMIN") {
    items.push({ label: "Admin", href: "/admin", testId: "global-admin-tab" });
  }
  return items;
}
