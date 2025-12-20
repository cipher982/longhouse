import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Sidebar } from '../src/components'

describe('Sidebar conversations', () => {
  it('renders conversation list when provided', () => {
    const conversations = [
      { id: 'c1', name: 'Conversation 1', meta: 'Updated today', active: true },
      { id: 'c2', name: 'Conversation 2', meta: 'Updated yesterday', active: false },
    ]

    render(
      <Sidebar
        conversations={conversations}
        isOpen={true}
        onToggle={() => {}}
        onNewConversation={() => {}}
        onClearAll={() => {}}
        onSelectConversation={() => {}}
      />
    )

    expect(screen.queryByText('No conversations yet')).toBeNull()
    expect(screen.getByText('Conversation 1')).toBeDefined()
    expect(screen.getByText('Conversation 2')).toBeDefined()
  })

  it('shows empty state when no conversations', () => {
    render(
      <Sidebar
        conversations={[]}
        isOpen={true}
        onToggle={() => {}}
        onNewConversation={() => {}}
        onClearAll={() => {}}
        onSelectConversation={() => {}}
      />
    )

    expect(screen.getByText('No conversations yet')).toBeDefined()
  })
})
