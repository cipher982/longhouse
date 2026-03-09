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
const ARTIFACT_DIR = path.resolve(
  process.env.PROVIDER_SMOKE_ARTIFACT_DIR?.trim() || path.join(E2E_DIR, 'test-results', 'provider-smoke'),
);
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

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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
    await delay(500);
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
    await delay(1000);
  }
  throw new Error(`Timed out waiting for ${label}. Last value: ${JSON.stringify(lastValue).slice(0, 2000)}`);
}

function createArtifactPaths(dir) {
  return {
    dir,
    manifest: path.join(dir, 'manifest.json'),
    backendLog: path.join(dir, 'backend.log'),
    browserLog: path.join(dir, 'browser.log'),
    failurePage: path.join(dir, 'failure-page.txt'),
    screenshot: path.join(dir, 'failure.png'),
  };
}

function resetDir(dir) {
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
}

function writeJson(filePath, value) {
  fs.writeFileSync(filePath, JSON.stringify(value, null, 2));
}

function writeText(filePath, value) {
  fs.writeFileSync(filePath, value, 'utf8');
}

function mirrorProcessStream(stream, logStream, outputStream) {
  stream.on('data', (chunk) => {
    logStream.write(chunk);
    outputStream.write(chunk);
  });
}

function spawnBackendProcess({ artifactPaths, anthropicApiKey, backendPort, backendUrl, claudeConfigDir, e2eDbDir }) {
  const logStream = fs.createWriteStream(artifactPaths.backendLog, { flags: 'a' });
  const processHandle = spawn('node', ['spawn-test-backend.js'], {
    cwd: E2E_DIR,
    stdio: ['ignore', 'pipe', 'pipe'],
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

  if (processHandle.stdout) mirrorProcessStream(processHandle.stdout, logStream, process.stdout);
  if (processHandle.stderr) mirrorProcessStream(processHandle.stderr, logStream, process.stderr);

  return { processHandle, logStream };
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
    [
      'run',
      'python',
      SHIP_SCRIPT,
      resolvedWorkspace,
      resolvedClaudeConfigDir,
      '--commis-id',
      'provider-smoke',
      '--continuation-kind',
      'local',
      '--origin-label',
      'Cinder',
    ],
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

  return {
    tempRoot,
    workspace: resolvedWorkspace,
    claudeConfigDir: resolvedClaudeConfigDir,
    rootSessionId,
    followupToken,
  };
}

function attachBrowserLogging(page, artifactPaths) {
  const logStream = fs.createWriteStream(artifactPaths.browserLog, { flags: 'a' });
  page.on('console', (message) => {
    logStream.write(`[console:${message.type()}] ${message.text()}\n`);
  });
  page.on('pageerror', (error) => {
    logStream.write(`[pageerror] ${error.stack || error.message}\n`);
  });
  page.on('requestfailed', (request) => {
    const failure = request.failure()?.errorText || 'unknown';
    logStream.write(`[requestfailed] ${request.method()} ${request.url()} :: ${failure}\n`);
  });
  return logStream;
}

async function recordFailurePage(browser, artifactPaths, manifest) {
  if (!browser) return;
  const pages = browser.contexts().flatMap((context) => context.pages());
  const page = pages[0];
  if (!page) return;
  try {
    await page.screenshot({ path: artifactPaths.screenshot, fullPage: true });
    manifest.final_url = page.url();
    writeText(artifactPaths.failurePage, await page.evaluate(() => document.body.textContent || ''));
  } catch {
    // best effort
  }
}

async function closeBrowser(browser) {
  if (!browser) return;
  await browser.close();
}

async function stopBackend(backend) {
  if (!backend || backend.exitCode !== null) return;
  backend.kill('SIGTERM');
  await delay(1000);
  if (backend.exitCode === null) backend.kill('SIGKILL');
}

async function main() {
  const artifactPaths = createArtifactPaths(ARTIFACT_DIR);
  resetDir(artifactPaths.dir);

  const manifest = {
    version: 1,
    status: 'running',
    backend: DEFAULT_BACKEND,
    model: DEFAULT_MODEL,
    started_at: new Date().toISOString(),
  };
  writeJson(artifactPaths.manifest, manifest);

  const anthropicApiKey = requireEnv('ANTHROPIC_API_KEY');
  const backendPort = Number.parseInt(process.env.E2E_BACKEND_PORT || '', 10) || randomPort();
  const backendUrl = `http://127.0.0.1:${backendPort}`;
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'lh-provider-smoke-'));
  const claudeConfigDir = path.join(tempRoot, 'claude');
  const e2eDbDir = path.join(tempRoot, 'dbs');
  fs.mkdirSync(claudeConfigDir, { recursive: true });
  fs.mkdirSync(e2eDbDir, { recursive: true });

  let backend;
  let backendLogStream;
  let browser;
  let browserLogStream;
  let seeded;

  try {
    ({ processHandle: backend, logStream: backendLogStream } = spawnBackendProcess({
      artifactPaths,
      anthropicApiKey,
      backendPort,
      backendUrl,
      claudeConfigDir,
      e2eDbDir,
    }));

    await waitForBackend(backendUrl);
    seeded = seedProviderSession({ anthropicApiKey, backendUrl, claudeConfigDir });
    Object.assign(manifest, {
      root_session_id: seeded.rootSessionId,
      root_origin_label: 'Cinder',
    });
    writeJson(artifactPaths.manifest, manifest);

    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    browserLogStream = attachBrowserLogging(page, artifactPaths);

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

    await pollUntil(
      async () => {
        const payload = await fetchJson(`${backendUrl}/api/agents/sessions/${childSessionId}/events?limit=200&branch_mode=all`);
        return (payload.events || []).map((event) => event.content_text || '').join('\n');
      },
      (text) => text.includes(seeded.followupToken),
      120_000,
      'follow-up token in child events',
    );

    const headSession = (thread.sessions || []).find((session) => session.id === thread.head_session_id);
    Object.assign(manifest, {
      status: 'success',
      finished_at: new Date().toISOString(),
      child_session_id: childSessionId,
      head_session_id: thread.head_session_id,
      head_origin_label: headSession?.origin_label || null,
      continuation_count: (thread.sessions || []).length,
      final_url: page.url(),
      created_continuation: childSessionId !== seeded.rootSessionId,
    });
    writeJson(artifactPaths.manifest, manifest);
    console.log(JSON.stringify(manifest, null, 2));
  } catch (error) {
    Object.assign(manifest, {
      status: 'failure',
      finished_at: new Date().toISOString(),
      error: error instanceof Error ? error.message : String(error),
      stack: error instanceof Error ? error.stack : null,
    });
    await recordFailurePage(browser, artifactPaths, manifest);
    writeJson(artifactPaths.manifest, manifest);
    throw error;
  } finally {
    await closeBrowser(browser);
    await stopBackend(backend);
    browserLogStream?.end();
    backendLogStream?.end();
    fs.rmSync(tempRoot, { recursive: true, force: true });
    if (seeded?.tempRoot) {
      fs.rmSync(seeded.tempRoot, { recursive: true, force: true });
    }
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exit(1);
});
