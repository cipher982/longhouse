import express from "express";
import cors from "cors";
import fetch from "node-fetch";
import dotenv from "dotenv";
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { promises as fs } from 'fs';
import path from 'path';
import { getRealtimeModel, getDefaultVoice } from '@jarvis/core';
dotenv.config();

// Model configuration loaded from shared config/models.json via @jarvis/core
// Override via env vars: JARVIS_REALTIME_MODEL, JARVIS_USE_MINI_MODEL, JARVIS_VOICE

function getVoice() {
  return getDefaultVoice();
}

// Nashville location data
const NASHVILLE_LOCATION = {
  lat: 36.1627,
  lon: -86.7816,
  accuracy: 5,
  speed: 0,
  course: 0,
  altitude: 182.5,
  timestamp: new Date().toISOString(),
  device_id: 2,
  battery: 85,
  address: "West Nashville, TN",
  valid: true,
  device_name: "david",
  device_unique_id: "33862011"
};

const app = express();
app.use(cors());
app.use(express.json());

function extractOutputText(responseJson) {
  if (!responseJson || typeof responseJson !== 'object') return null;
  if (typeof responseJson.output_text === 'string' && responseJson.output_text.trim()) {
    return responseJson.output_text.trim();
  }
  const output = responseJson.output;
  if (!Array.isArray(output)) return null;
  for (const item of output) {
    const content = item?.content;
    if (!Array.isArray(content)) continue;
    for (const part of content) {
      const text = part?.text;
      if (typeof text === 'string' && text.trim()) return text.trim();
    }
  }
  return null;
}

function safeParseJsonObject(text) {
  if (typeof text !== 'string') return null;
  try {
    return JSON.parse(text);
  } catch {
    // Try to recover a JSON object substring.
    const start = text.indexOf('{');
    const end = text.lastIndexOf('}');
    if (start >= 0 && end > start) {
      try {
        return JSON.parse(text.slice(start, end + 1));
      } catch {
        return null;
      }
    }
    return null;
  }
}

function normalizeTitleMessages(messages) {
  if (!Array.isArray(messages)) return [];
  const cleaned = [];
  for (const m of messages) {
    const role = m?.role;
    const content = typeof m?.content === 'string' ? m.content.trim() : '';
    if ((role !== 'user' && role !== 'assistant') || !content) continue;
    // Hard cap per message to keep requests small and predictable.
    cleaned.push({ role, content: content.slice(0, 800) });
    if (cleaned.length >= 12) break;
  }
  return cleaned;
}

// Generate a short conversation title (user + assistant)
app.post("/conversation/title", async (req, res) => {
  try {
    if (!process.env.OPENAI_API_KEY) {
      return res.status(500).json({ error: "missing_openai_api_key" });
    }

    const messages = normalizeTitleMessages(req.body?.messages);
    if (messages.length < 2) {
      return res.status(400).json({ error: "messages_required" });
    }
    const hasUser = messages.some(m => m.role === 'user');
    const hasAssistant = messages.some(m => m.role === 'assistant');
    if (!hasUser || !hasAssistant) {
      return res.status(400).json({ error: "need_user_and_assistant_messages" });
    }

    const model = process.env.JARVIS_TITLE_MODEL || "gpt-5-mini";
    const reasoningEffort = process.env.JARVIS_TITLE_REASONING_EFFORT || "minimal";
    const system = [
      "Generate a short, helpful conversation title based on the transcript.",
      "Use Title Case, 3–8 words.",
      "No quotes, no trailing punctuation, no dates/times.",
      "Avoid generic titles like \"Conversation\" or \"Chat\"."
    ].join(" ");

    const transcript = messages
      .map(m => `${m.role === 'user' ? 'User' : 'Assistant'}: ${m.content}`)
      .join("\n");

    const payload = {
      model,
      reasoning: { effort: reasoningEffort },
      input: [
        { role: "system", content: [{ type: "input_text", text: system }] },
        { role: "user", content: [{ type: "input_text", text: transcript }] },
      ],
      max_output_tokens: 200,
      text: {
        verbosity: "low",
        format: {
          type: "json_schema",
          name: "conversation_title",
          strict: true,
          schema: {
            type: "object",
            additionalProperties: false,
            properties: {
              title: { type: "string" }
            },
            required: ["title"]
          }
        }
      }
    };

    async function callOpenAI(p) {
      const r = await fetch("https://api.openai.com/v1/responses", {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${process.env.OPENAI_API_KEY}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify(p)
      });
      const js = await r.json();
      return { r, js };
    }

    let { r, js } = await callOpenAI(payload);
    if (!r.ok) {
      console.error("Title generation error:", js);
      return res.status(500).json({ error: "title_generation_failed", details: js });
    }

    let outputText = extractOutputText(js);
    // GPT-5 family may spend output budget on reasoning; retry once with a higher cap.
    if (!outputText && js?.status === "incomplete" && js?.incomplete_details?.reason === "max_output_tokens") {
      const retryPayload = { ...payload, max_output_tokens: 400 };
      ({ r, js } = await callOpenAI(retryPayload));
      if (r.ok) outputText = extractOutputText(js);
    }

    const parsed = safeParseJsonObject(outputText);
    const title = typeof parsed?.title === "string" ? parsed.title.trim() : null;
    if (!title) {
      console.warn("Title generation returned unexpected output:", { outputText });
      return res.status(500).json({ error: "bad_title_output", outputText });
    }

    return res.json({ title });
  } catch (e) {
    console.error(e);
    return res.status(500).json({ error: "title_failed" });
  }
});

// MCP client connections
const mcpClients = new Map();

// Initialize MCP clients on startup
const SHOULD_INIT_MCP = process.env.SKIP_MCP !== '1';

async function initMCPClients() {
  if (!SHOULD_INIT_MCP) {
    console.log('⚠️  SKIP_MCP=1, skipping MCP client initialization');
    return;
  }
  try {
    // Traccar location MCP server
    const traccarTransport = new StdioClientTransport({
      command: 'uvx',
      args: ['--from', '/Users/davidrose/git/traccar', 'traccar-mcp']
    });

    const traccarClient = new Client(
      { name: 'jarvis-server', version: '1.0.0' },
      { capabilities: { tools: {} } }
    );

    await traccarClient.connect(traccarTransport);
    mcpClients.set('traccar', traccarClient);
    console.log('✅ Traccar MCP connected');

    // WHOOP health MCP server
    const whoopTransport = new StdioClientTransport({
      command: 'uvx',
      args: ['--from', '/Users/davidrose/git/whoop-mcp', 'whoop-mcp']
    });

    const whoopClient = new Client(
      { name: 'jarvis-server', version: '1.0.0' },
      { capabilities: { tools: {} } }
    );

    await whoopClient.connect(whoopTransport);
    mcpClients.set('whoop', whoopClient);
    console.log('✅ WHOOP MCP connected');

    console.log('✅ All MCP clients initialized:', Array.from(mcpClients.keys()));
  } catch (error) {
    console.error('❌ Failed to initialize MCP clients:', error);
  }
}

// Mint an ephemeral Realtime session token for the browser PWA
app.get("/session", async (req, res) => {
  try {
    const r = await fetch("https://api.openai.com/v1/realtime/client_secrets", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${process.env.OPENAI_API_KEY}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        session: {
          type: "realtime",
          model: getRealtimeModel(),
          audio: {
            output: { voice: getVoice() }
          }
        }
      })
    });
    const js = await r.json();
    if (!r.ok) {
      console.error("Realtime session error:", js);
      return res.status(500).json(js);
    }
    res.json(js);
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: "session_failed" });
  }
});

// Enhanced tool endpoint with MCP integration
app.post("/tool", async (req, res) => {
  const { name, args } = req.body || {};
  console.log(`🔧 Tool call: ${name}`, args);

  try {
    // Location tools via traccar MCP
    if (name === "location.get_current") {
      // Real location via Traccar MCP
      const client = mcpClients.get('traccar');
      if (!client) {
        return res.status(500).json({ error: 'Traccar MCP client not available' });
      }
      const result = await client.callTool({
        name: 'get_current_location',
        arguments: args || {}
      });
      console.log('📍 Location data:', result);
      return res.json(result.content?.[0]?.text ? JSON.parse(result.content[0].text) : result);
    }

    if (name === "location.get_history") {
      // Real location history via Traccar MCP
      const client = mcpClients.get('traccar');
      if (!client) {
        return res.status(500).json({ error: 'Traccar MCP client not available' });
      }
      const result = await client.callTool({
        name: 'get_location_history',
        arguments: args || {}
      });
      return res.json(result.content?.[0]?.text ? JSON.parse(result.content[0].text) : result);
    }

    // WHOOP health data via WHOOP MCP
    if (name === "whoop.get_daily") {
      const client = mcpClients.get('whoop');
      if (!client) {
        return res.status(500).json({ error: 'WHOOP MCP client not available' });
      }
      const result = await client.callTool({
        name: 'get_health_status',
        arguments: args || {}
      });
      console.log('💪 WHOOP data:', result);
      return res.json(result.content?.[0]?.text ? JSON.parse(result.content[0].text) : result);
    }

    if (name === "whoop.get_recovery_trend") {
      const client = mcpClients.get('whoop');
      if (!client) {
        return res.status(500).json({ error: 'WHOOP MCP client not available' });
      }
      const result = await client.callTool({
        name: 'get_recovery_trend',
        arguments: args || {}
      });
      return res.json(result.content?.[0]?.text ? JSON.parse(result.content[0].text) : result);
    }

    if (name === "whoop.get_workouts") {
      const client = mcpClients.get('whoop');
      if (!client) {
        return res.status(500).json({ error: 'WHOOP MCP client not available' });
      }
      const result = await client.callTool({
        name: 'get_recent_workouts',
        arguments: args || {}
      });
      return res.json(result.content?.[0]?.text ? JSON.parse(result.content[0].text) : result);
    }

    return res.json({ ok: true, echo: { name, args } });
  } catch (error) {
    console.error('❌ Tool execution failed:', error);
    return res.status(500).json({
      error: 'Tool execution failed',
      details: error.message
    });
  }
});

// ----------------------------------------------------------------------------
// Simple Sync Service (in-memory dev scaffold)
// POST /sync/push  { deviceId, cursor, ops: [{ opId, type, body, lamport, ts }] }
// GET  /sync/pull?cursor=0 -> { ops, nextCursor }
// ----------------------------------------------------------------------------

// Durable op log (file-backed for dev). Upgradable to Postgres later.
const dataDir = path.join(process.cwd(), 'server', 'data');
const opsFile = path.join(dataDir, 'ops.json');
let opsLog = [];
const seenOpIds = new Set();

async function loadOps() {
  try {
    await fs.mkdir(dataDir, { recursive: true });
    const buf = await fs.readFile(opsFile, 'utf8');
    const parsed = JSON.parse(buf);
    if (Array.isArray(parsed)) {
      opsLog = parsed;
      for (const op of opsLog) seenOpIds.add(op.opId);
    }
    console.log(`🗃️  Loaded ${opsLog.length} ops from disk`);
  } catch (e) {
    // First run or unreadable file – start empty
    opsLog = [];
    console.log('🗃️  No existing op log, starting fresh');
  }
}

async function saveOps() {
  try {
    await fs.mkdir(dataDir, { recursive: true });
    await fs.writeFile(opsFile, JSON.stringify(opsLog, null, 2), 'utf8');
  } catch (e) {
    console.error('Failed to persist ops:', e.message);
  }
}

app.post('/sync/push', async (req, res) => {
  try {
    const { deviceId, cursor, ops } = req.body || {};
    if (!Array.isArray(ops)) return res.status(400).json({ error: 'ops_required' });

    const acked = [];
    for (const op of ops) {
      if (!op?.opId) continue;
      if (seenOpIds.has(op.opId)) {
        acked.push(op.opId);
        continue; // idempotent
      }
      seenOpIds.add(op.opId);
      // Normalize ts to ISO string
      const ts = op.ts && typeof op.ts === 'string' ? op.ts : new Date().toISOString();
      opsLog.push({ ...op, ts });
      acked.push(op.opId);
    }

    const nextCursor = opsLog.length;
    // Fire-and-forget persist
    saveOps();
    return res.json({ acked, nextCursor });
  } catch (e) {
    console.error('sync push error:', e);
    return res.status(500).json({ error: 'push_failed' });
  }
});

app.get('/sync/pull', async (req, res) => {
  try {
    const cursor = Number(req.query.cursor || 0);
    if (Number.isNaN(cursor) || cursor < 0) return res.status(400).json({ error: 'bad_cursor' });
    const ops = opsLog.slice(cursor);
    const nextCursor = opsLog.length;
    return res.json({ ops, nextCursor });
  } catch (e) {
    console.error('sync pull error:', e);
    return res.status(500).json({ error: 'pull_failed' });
  }
});

const port = process.env.PORT || 8787;
const host = process.env.HOST || '0.0.0.0';

// Graceful shutdown handler
async function shutdown(signal) {
  console.log(`\n🛑 Received ${signal}, shutting down gracefully...`);

  // Close MCP connections
  for (const [name, client] of mcpClients) {
    try {
      await client.close();
      console.log(`✅ Closed MCP client: ${name}`);
    } catch (error) {
      console.error(`❌ Error closing MCP client ${name}:`, error.message);
    }
  }

  process.exit(0);
}

// Register signal handlers
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

// Initialize MCP clients then start server
loadOps().then(() => initMCPClients()).then(() => {
  const server = app.listen(port, host, () => {
    console.log(`🚀 Jarvis server listening on ${host}:${port}`);
  });

  // Store server reference for potential cleanup
  process.server = server;
}).catch(error => {
  console.error('❌ Failed to start server:', error);
  process.exit(1);
});
