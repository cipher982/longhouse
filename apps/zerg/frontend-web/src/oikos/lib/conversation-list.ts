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

  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  if (!Number.isFinite(diffMs) || diffMs < 0) {
    return `Updated ${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`
  }

  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto', style: 'short' })
  const seconds = Math.floor(diffMs / 1000)
  if (seconds < 45) return 'Just now'

  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return rtf.format(-minutes, 'minute')

  const hours = Math.floor(minutes / 60)
  if (hours < 24) return rtf.format(-hours, 'hour')

  const days = Math.floor(hours / 24)
  if (days < 7) return rtf.format(-days, 'day')

  // Beyond a week, switch to calendar dates for quick scanning.
  const isSameYear = now.getFullYear() === date.getFullYear()
  const fmt = new Intl.DateTimeFormat(undefined, isSameYear ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' })
  return `Updated ${fmt.format(date)}`
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
