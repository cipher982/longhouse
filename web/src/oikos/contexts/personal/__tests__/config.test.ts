import { afterEach, describe, expect, it } from 'vitest'

import { bootstrapStore, type BootstrapData } from '../../../lib/bootstrap-store'
import { personalConfig } from '../config'

function makeBootstrap(prompt: string): BootstrapData {
  return {
    prompt,
    enabled_tools: [],
    user_context: {},
    available_models: [],
    preferences: {
      chat_model: 'gpt-4.1',
      reasoning_effort: 'low',
    },
  }
}

describe('personalConfig instructions', () => {
  afterEach(() => {
    bootstrapStore.reset()
  })

  it('prefers the server bootstrap prompt when available', () => {
    bootstrapStore.setBootstrap(makeBootstrap('Use the server prompt'))

    expect(personalConfig.instructions).toBe('Use the server prompt')
  })

  it('falls back to generated local instructions when bootstrap is absent', () => {
    bootstrapStore.reset()

    expect(personalConfig.instructions).toContain('This Realtime session is I/O ONLY.')
  })
})
