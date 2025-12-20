#!/usr/bin/env node

/**
 * Copy design tokens from shared package into Jarvis styles folder.
 * This ensures tokens work in both Docker and local development.
 */

import { copyFile, mkdir } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const webRoot = resolve(__dirname, '..')
const tokensDir = resolve(webRoot, 'styles/tokens')

// Try multiple source locations (Docker vs local)
const possibleSources = [
  resolve(__dirname, '../../../../../packages/design-tokens/dist'),  // Local: apps/jarvis/apps/web/scripts -> repo root/packages
  resolve(__dirname, '../../../swarm-packages/design-tokens/dist'),  // Docker: /app/apps/web/scripts -> /app/swarm-packages
]

const files = ['core.css', 'theme-glass.css']

async function copyTokens() {
  // Find valid source directory
  let sourceDir = null
  for (const dir of possibleSources) {
    if (existsSync(resolve(dir, 'core.css'))) {
      sourceDir = dir
      break
    }
  }

  if (!sourceDir) {
    console.error('❌ Could not find design tokens. Run "make build-tokens" first.')
    process.exit(1)
  }

  // Create output directory
  await mkdir(tokensDir, { recursive: true })

  // Copy files
  for (const file of files) {
    const src = resolve(sourceDir, file)
    const dest = resolve(tokensDir, file)
    await copyFile(src, dest)
    console.log(`✅ Copied ${file}`)
  }

  console.log('🎨 Design tokens copied to styles/tokens/')
}

copyTokens().catch(err => {
  console.error('Failed to copy tokens:', err)
  process.exit(1)
})
