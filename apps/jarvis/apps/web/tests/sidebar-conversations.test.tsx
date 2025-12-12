import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { act } from 'react'
import { AppProvider } from '../src/context'
import App from '../src/App'
import { stateManager } from '../lib/state-manager'

describe('Sidebar conversations', () => {
  it('renders conversation list from stateManager events', async () => {
    render(
      <AppProvider>
        <App />
      </AppProvider>
    )

    expect(screen.getByText('No conversations yet')).toBeDefined()

    await act(async () => {
      stateManager.setConversations([
        { id: 'c1', name: 'Conversation 1', meta: 'Updated today', active: true },
        { id: 'c2', name: 'Conversation 2', meta: 'Updated yesterday', active: false },
      ])
    })

    expect(screen.queryByText('No conversations yet')).toBeNull()
    expect(screen.getByText('Conversation 1')).toBeDefined()
    expect(screen.getByText('Conversation 2')).toBeDefined()
  })
})
