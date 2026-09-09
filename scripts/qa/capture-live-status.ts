#!/usr/bin/env bun
/** Read-only local design capture. Never commit its output: it contains private session history.
 * bun scripts/qa/capture-live-status.ts SESSION_ID [--output PATH] [--seconds 20]
 * Uses the same linked Runtime Host/device identity as make dev; performs GETs only.
 */
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, resolve } from "node:path";

const args = process.argv.slice(2);
const sessionId = args[0];
function option(name: string, fallback: string): string {
  const index = args.indexOf(name);
  return index < 0 ? fallback : args[index + 1] ?? fallback;
}
if (!sessionId || sessionId.startsWith("--")) {
  throw new Error("Usage: bun scripts/qa/capture-live-status.ts SESSION_ID [--output PATH] [--seconds 20]");
}
const seconds = Number(option("--seconds", "20"));
if (!Number.isFinite(seconds) || seconds < 0 || seconds > 120) throw new Error("--seconds must be between 0 and 120");
const output = resolve(option("--output", `artifacts/live-status-lab/${sessionId}.json`));
const host = (await readFile(`${homedir()}/.longhouse/machine/target-url`, "utf8")).trim().replace(/\/$/, "");
const token = (await readFile(`${homedir()}/.longhouse/machine/device-token`, "utf8")).trim();
if (!host || !token) throw new Error("Link this machine with longhouse auth before capturing.");
const headers = { Authorization: `Bearer ${token}` };
const path = `/api/timeline/sessions/${encodeURIComponent(sessionId)}/workspace`;
const response = await fetch(`${host}${path}?limit=100&branch_mode=head`, { headers, signal: AbortSignal.timeout(30_000) });
if (!response.ok) throw new Error(`Workspace capture failed: HTTP ${response.status}`);
const workspace = await response.json();
const capturedAt = new Date().toISOString();
const stream: Array<{ atMs: number; event: string; data: unknown }> = [];
let streamError: string | null = null;
if (seconds > 0) {
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), seconds * 1000);
  const start = performance.now();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    const live = await fetch(`${host}${path}/stream`, { headers, signal: abort.signal });
    if (!live.ok || !live.body) throw new Error(`HTTP ${live.status}`);
    for await (const bytes of live.body) {
      buffer += decoder.decode(bytes, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = /^event:\s*(.+)$/m.exec(frame)?.[1];
        const data = frame.split("\n").filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
        if (event && data) stream.push({ atMs: Math.round(performance.now() - start), event, data: JSON.parse(data) });
      }
    }
  } catch (error) {
    if (!abort.signal.aborted) streamError = error instanceof Error ? error.message : String(error);
  } finally {
    clearTimeout(timeout);
    abort.abort();
  }
}
const capture = {
  schema: "longhouse.live-status-capture.v1",
  capturedAt,
  source: { kind: "workspace-snapshot", url: `${host}${path}`, sessionId },
  workspace,
  stream,
  streamError,
  note: "Recorded content and served facts. The design lab labels any injected states and synthetic replay cadence; they are not observed provider timing.",
};
await mkdir(dirname(output), { recursive: true });
await writeFile(output, JSON.stringify(capture, null, 2), { mode: 0o600 });
console.log(JSON.stringify({ output, sessionId, items: workspace.projection?.items?.length, streamFrames: stream.length, streamError }, null, 2));
