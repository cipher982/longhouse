#!/usr/bin/env node
import { spawn, execFileSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { chromium } from 'playwright';
import { fileURLToPath } from 'url';
import { randomUUID } from 'crypto';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const E2E_DIR = path.resolve(__dirname, '..');
const BACKEND_DIR = path.resolve(E2E_DIR, '../backend');
const SHIP_SCRIPT = path.join(BACKEND_DIR, 'scripts', 'ship_claude_session.py');
const ARTIFACT_DIR = path.join(E2E_DIR, 'test-results', 'provider-smoke');
const DEFAULT_MODEL = process.env.SESSION_CHAT_MODEL?.trim() || 'claude-sonnet-4-20250514';
const DEFAULT_BACKEND = process.env.SESSION_CHAT_BACKEND?.trim() || 'anthropic';

function requireEnv(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required for provider continuation smoke`);
  return value;
}

function randomPort() {
  return 30000 + Math.floor(Math.random() * 30000);
}

async function waitForBackend(url, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`${url}/api/health/db`);
      if (resp.ok) return;
    } catch {
      // keep polling
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`Backend did not become healthy at ${url} within ${timeoutMs}ms`);
}

async function fetchJson(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`GET ${url} failed with ${resp.status}`);
  }
  return await resp.json();
}

async function pollUntil(fn, predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let lastValue;
  while (Date.now() < deadline) {
    lastValue = await fn();
    if (predicate(lastValue)) return lastValue;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error(`Timed out waiting for ${label}. Last value: ${JSON.stringify(lastValue).slice(0, 2000)}`);
}

function ensureArtifactsDir() {
  fs.rmSync(ARTIFACT_DIR, { recursive: true, force: true });
  fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
}

function seedProviderSession({ anthropicApiKey, backendUrl, claudeConfigDir }) {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'lh-provider-continuation-'));
  const workspace = path.join(tempRoot, 'workspace');
  fs.mkdirSync(workspace, { recursive: true });
  fs.mkdirSync(claudeConfigDir, { recursive: true });

  const resolvedWorkspace = fs.realpathSync(workspace);
  const resolvedClaudeConfigDir = fs.realpathSync(claudeConfigDir);
  const seedToken = `seed-ok-${randomUUID().slice(0, 8)}`;
  const followupToken = `followup-ok-${randomUUID().slice(0, 8)}`;

  const promptOutput = execFileSync(
    'claude',
    ['-p', `Reply with exactly: ${seedToken}`, '--output-format', 'stream-json', '--verbose', '--print'],
    {
      cwd: resolvedWorkspace,
      env: {
        ...process.env,
        ANTHROPIC_API_KEY: anthropicApiKey,
        ANTHROPIC_MODEL: DEFAULT_MODEL,
        CLAUDE_CONFIG_DIR: resolvedClaudeConfigDir,
      },
      encoding: 'utf8',
      maxBuffer: 8 * 1024 * 1024,
      timeout: 180_000,
    },
  );

  if (!promptOutput.includes(seedToken)) {
    throw new Error(`Seed prompt did not produce expected token ${seedToken}`);
  }

  const rootSessionId = execFileSync(
    'uv',
    ['run', 'python', SHIP_SCRIPT, resolvedWorkspace, resolvedClaudeConfigDir, '--commis-id', 'provider-smoke', '--continuation-kind', 'local', '--origin-label', 'Cinder'],
    {
      cwd: BACKEND_DIR,
      env: {
        ...process.env,
        LONGHOUSE_API_URL: backendUrl,
        CLAUDE_CONFIG_DIR: resolvedClaudeConfigDir,
      },
      encoding: 'utf8',
      maxBuffer: 1024 * 1024,
      timeout: 120_000,
    },
  ).trim();

  if (!rootSessionId) throw new Error('Failed to ship seeded Claude session into Longhouse');

  return { rootSessionId, followupToken, claudeConfigDir: resolvedClaudeConfigDir, workspace: resolvedWorkspace };
}

async function main() {
  ensureArtifactsDir();
  const anthropicApiKey = requireEnv('ANTHROPIC_API_KEY');
  const backendPort = Number.parseInt(process.env.E2E_BACKEND_PORT || '', 10) || randomPort();
  const backendUrl = `http://127.0.0.1:${backendPort}`;
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'lh-provider-smoke-'));
  const claudeConfigDir = path.join(tempRoot, 'claude');
  const e2eDbDir = path.join(tempRoot, 'dbs');
  fs.mkdirSync(claudeConfigDir, { recursive: true });
  fs.mkdirSync(e2eDbDir, { recursive: true });

  let backend;
  let browser;
  try {
    backend = spawn('node', ['spawn-test-backend.js'], {
      cwd: E2E_DIR,
      stdio: 'inherit',
      env: {
        ...process.env,
        BACKEND_PORT: String(backendPort),
        LONGHOUSE_API_URL: backendUrl,
        CLAUDE_CONFIG_DIR: claudeConfigDir,
        E2E_DB_DIR: e2eDbDir,
        E2E_FAKE_SESSION_CHAT: '0',
        SESSION_CHAT_BACKEND: DEFAULT_BACKEND,
        SESSION_CHAT_MODEL: DEFAULT_MODEL,
        ANTHROPIC_API_KEY: anthropicApiKey,
      },
    });

    await waitForBackend(backendUrl);
    const seeded = seedProviderSession({ anthropicApiKey, backendUrl, claudeConfigDir });

    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    await page.goto(`${backendUrl}/timeline/${seeded.rootSessionId}?resume=1`);
    await page.waitForSelector('body[data-ready="true"]', { timeout: 30_000 });
    await page.locator('.session-chat-composer textarea').fill(`Reply with exactly: ${seeded.followupToken}`);
    await page.getByRole('button', { name: 'Send' }).click();

    await page.waitForFunction(
      (rootId) => window.location.pathname !== `/timeline/${rootId}`,
      seeded.rootSessionId,
      { timeout: 120_000 },
    );

    const childSessionId = new URL(page.url()).pathname.split('/').pop();
    if (!childSessionId || childSessionId === seeded.rootSessionId) {
      throw new Error(`Expected child session navigation away from ${seeded.rootSessionId}, got ${page.url()}`);
    }

    const thread = await pollUntil(
      () => fetchJson(`${backendUrl}/api/agents/sessions/${seeded.rootSessionId}/thread`),
      (payload) => payload.head_session_id === childSessionId,
      120_000,
      'thread head update',
    );

    const childEvents = await pollUntil(
      async () => {
        const payload = await fetchJson(`${backendUrl}/api/agents/sessions/${childSessionId}/events?limit=200&branch_mode=all`);
        return (payload.events || []).map((event) => event.content_text || '').join('\n');
      },
      (text) => text.includes(seeded.followupToken),
      120_000,
      'follow-up token in child events',
    );

    const proof = {
      backendUrl,
      rootSessionId: seeded.rootSessionId,
      childSessionId,
      followupToken: seeded.followupToken,
      headSessionId: thread.head_session_id,
      continuationCount: (thread.sessions || []).length,
      childEventExcerpt: childEvents.slice(-500),
      finalUrl: page.url(),
    };
    fs.writeFileSync(path.join(ARTIFACT_DIR, 'proof.json'), JSON.stringify(proof, null, 2));
    console.log(JSON.stringify(proof, null, 2));
  } catch (error) {
    const failure = {
      error: error instanceof Error ? error.message : String(error),
      stack: error instanceof Error ? error.stack : null,
    };
    if (browser) {
      const pages = browser.contexts().flatMap((context) => context.pages());
      if (pages[0]) {
        try {
          await pages[0].screenshot({ path: path.join(ARTIFACT_DIR, 'failure.png'), fullPage: true });
          failure.url = pages[0].url();
          failure.bodyText = await pages[0].evaluate(() => document.body.textContent || '');
        } catch {
          // best effort
        }
      }
    }
    fs.writeFileSync(path.join(ARTIFACT_DIR, 'failure.json'), JSON.stringify(failure, null, 2));
    throw error;
  } finally {
    if (browser) await browser.close();
    if (backend && backend.exitCode === null) {
      backend.kill('SIGTERM');
      await new Promise((resolve) => setTimeout(resolve, 1000));
      if (backend.exitCode === null) backend.kill('SIGKILL');
    }
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exit(1);
});
