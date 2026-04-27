#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";

function usage() {
  console.error(`Usage:
  scripts/ops/session-transcript-timing.mjs <session-id-or-transcript-path> [--json]

Analyzes a local Claude JSONL transcript and separates:
  - assistant tool call -> tool result latency
  - tool result -> next assistant latency
`);
}

function parseArgs(argv) {
  const args = [...argv];
  let json = false;
  const positional = [];
  while (args.length > 0) {
    const arg = args.shift();
    if (arg === "--json") {
      json = true;
    } else if (arg === "-h" || arg === "--help") {
      usage();
      process.exit(0);
    } else if (arg?.startsWith("--")) {
      throw new Error(`Unknown option: ${arg}`);
    } else if (arg) {
      positional.push(arg);
    }
  }
  if (positional.length !== 1) {
    usage();
    process.exit(2);
  }
  return { target: positional[0], json };
}

function expandHome(value) {
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.join(os.homedir(), value.slice(2));
  return value;
}

function findTranscript(target) {
  const expanded = expandHome(target);
  if (fs.existsSync(expanded) && fs.statSync(expanded).isFile()) {
    return expanded;
  }

  const sessionId = target;
  const projectsRoot = path.join(os.homedir(), ".claude", "projects");
  const matches = [];
  for (const projectDir of fs.readdirSync(projectsRoot, { withFileTypes: true })) {
    if (!projectDir.isDirectory()) continue;
    const candidate = path.join(projectsRoot, projectDir.name, `${sessionId}.jsonl`);
    if (fs.existsSync(candidate)) matches.push(candidate);
  }
  if (matches.length === 0) {
    throw new Error(`No Claude transcript found for ${target}`);
  }
  if (matches.length > 1) {
    throw new Error(`Multiple Claude transcripts found for ${target}:\n${matches.join("\n")}`);
  }
  return matches[0];
}

function contentArray(row) {
  return Array.isArray(row?.message?.content) ? row.message.content : [];
}

function eventKind(row) {
  if (row?.type === "assistant") {
    const toolUse = contentArray(row).find((item) => item?.type === "tool_use");
    if (toolUse) return `assistant_tool:${toolUse.name || "unknown"}`;
    if (contentArray(row).some((item) => item?.type === "text")) return "assistant_text";
    return "assistant";
  }
  if (row?.type === "user") {
    if (contentArray(row).some((item) => item?.type === "tool_result")) return "tool_result";
    return "user";
  }
  return row?.type || "other";
}

function quantiles(values) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (sorted.length === 0) return { n: 0 };
  const pick = (fraction) => sorted[Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * fraction))];
  const avg = sorted.reduce((sum, value) => sum + value, 0) / sorted.length;
  return {
    n: sorted.length,
    min: Number(pick(0).toFixed(3)),
    p50: Number(pick(0.5).toFixed(3)),
    p90: Number(pick(0.9).toFixed(3)),
    max: Number(pick(1).toFixed(3)),
    avg: Number(avg.toFixed(3)),
  };
}

async function readRows(transcriptPath) {
  const rows = [];
  const input = fs.createReadStream(transcriptPath, { encoding: "utf8" });
  const rl = readline.createInterface({ input, crlfDelay: Infinity });
  let lineNo = 0;
  for await (const line of rl) {
    lineNo += 1;
    if (!line.trim()) continue;
    try {
      const row = JSON.parse(line);
      if (row.timestamp) rows.push({ ...row, _line: lineNo });
    } catch {
      // Ignore partial/corrupt lines in active transcripts.
    }
  }
  rows.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
  return rows;
}

function summarize(rows, transcriptPath) {
  const toolExecution = [];
  const modelAfterTool = [];
  const textToTool = [];
  const allGaps = [];
  const assistants = [];
  const recent = [];

  for (const row of rows) {
    if (row.type === "assistant" && row.message?.usage) {
      assistants.push({
        line: row._line,
        timestamp: row.timestamp,
        kind: eventKind(row),
        model: row.message.model,
        cache_read_input_tokens: row.message.usage.cache_read_input_tokens || 0,
        cache_creation_input_tokens: row.message.usage.cache_creation_input_tokens || 0,
        output_tokens: row.message.usage.output_tokens || 0,
        service_tier: row.message.usage.service_tier || "",
        speed: row.message.usage.speed || "",
      });
    }
  }

  for (let index = 0; index < rows.length - 1; index += 1) {
    const current = rows[index];
    const next = rows[index + 1];
    const currentKind = eventKind(current);
    const nextKind = eventKind(next);
    const delta = (new Date(next.timestamp) - new Date(current.timestamp)) / 1000;
    allGaps.push(delta);
    if (currentKind.startsWith("assistant_tool") && nextKind === "tool_result") toolExecution.push(delta);
    if (currentKind === "tool_result" && nextKind.startsWith("assistant")) modelAfterTool.push(delta);
    if (currentKind === "assistant_text" && nextKind.startsWith("assistant_tool")) textToTool.push(delta);
  }

  for (const row of rows.slice(-30)) {
    const usage = row.message?.usage;
    recent.push({
      line: row._line,
      timestamp: row.timestamp,
      kind: eventKind(row),
      cache_read_input_tokens: usage?.cache_read_input_tokens || 0,
      output_tokens: usage?.output_tokens || 0,
    });
  }

  return {
    transcript_path: transcriptPath,
    events: rows.length,
    first_timestamp: rows[0]?.timestamp || null,
    last_timestamp: rows.at(-1)?.timestamp || null,
    models: [...new Set(assistants.map((row) => row.model).filter(Boolean))],
    service_tiers: [...new Set(assistants.map((row) => row.service_tier).filter(Boolean))],
    speeds: [...new Set(assistants.map((row) => row.speed).filter(Boolean))],
    max_cache_read_input_tokens: Math.max(0, ...assistants.map((row) => row.cache_read_input_tokens)),
    last_cache_read_input_tokens: assistants.at(-1)?.cache_read_input_tokens || 0,
    latency_seconds: {
      assistant_tool_to_tool_result: quantiles(toolExecution),
      tool_result_to_next_assistant: quantiles(modelAfterTool),
      assistant_text_to_assistant_tool: quantiles(textToTool),
      all_adjacent_events: quantiles(allGaps),
    },
    recent_events: recent,
  };
}

function printText(summary) {
  console.log(`transcript: ${summary.transcript_path}`);
  console.log(`events: ${summary.events}`);
  console.log(`first: ${summary.first_timestamp}`);
  console.log(`last: ${summary.last_timestamp}`);
  console.log(`models: ${summary.models.join(", ") || "unknown"}`);
  console.log(`service_tiers: ${summary.service_tiers.join(", ") || "unknown"}`);
  console.log(`speeds: ${summary.speeds.join(", ") || "unknown"}`);
  console.log(`last_cache_read_input_tokens: ${summary.last_cache_read_input_tokens}`);
  console.log(`max_cache_read_input_tokens: ${summary.max_cache_read_input_tokens}`);
  console.log("");
  for (const [name, stats] of Object.entries(summary.latency_seconds)) {
    console.log(`${name}: ${JSON.stringify(stats)}`);
  }
  console.log("\nrecent events:");
  for (const row of summary.recent_events) {
    const cache = row.cache_read_input_tokens ? ` cache=${row.cache_read_input_tokens}` : "";
    const out = row.output_tokens ? ` out=${row.output_tokens}` : "";
    console.log(`${row.line}\t${row.timestamp}\t${row.kind}${cache}${out}`);
  }
}

try {
  const { target, json } = parseArgs(process.argv.slice(2));
  const transcriptPath = findTranscript(target);
  const rows = await readRows(transcriptPath);
  const summary = summarize(rows, transcriptPath);
  if (json) {
    console.log(JSON.stringify(summary, null, 2));
  } else {
    printText(summary);
  }
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
