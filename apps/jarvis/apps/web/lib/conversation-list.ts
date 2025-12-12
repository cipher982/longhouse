export interface RawConversation {
  id: string
  name: string
  createdAt: Date | string
  updatedAt: Date | string
}

export interface SidebarConversation {
  id: string
  name: string
  meta: string
  active?: boolean
}

function formatMeta(updatedAt: Date | string): string {
  const date = typeof updatedAt === 'string' ? new Date(updatedAt) : updatedAt
  if (Number.isNaN(date.getTime())) return 'Updated recently'
  return `Updated ${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`
}

export function toSidebarConversations(
  conversations: RawConversation[],
  activeConversationId: string | null
): SidebarConversation[] {
  return conversations.map((c) => ({
    id: c.id,
    name: c.name || 'Untitled',
    meta: formatMeta(c.updatedAt),
    active: activeConversationId ? c.id === activeConversationId : false,
  }))
}
