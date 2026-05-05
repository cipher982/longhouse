#!/usr/bin/env bun
/**
 * Build a small local review page for a qa-ui-workbench run.
 *
 * Usage:
 *   bunx tsx scripts/ui-workbench-report.ts artifacts/ui-capture/workbench-...
 */

import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from "fs";
import path from "path";

interface Manifest {
  timestamp?: string;
  scene?: string;
  errors?: string[];
  git?: {
    sha?: string;
    branch?: string;
    dirty?: boolean;
  };
  config?: {
    viewport_name?: string;
    viewport?: {
      width?: number;
      height?: number;
      deviceScaleFactor?: number;
    };
  };
  artifacts?: Record<string, unknown>;
}

interface CaptureCard {
  name: string;
  relDir: string;
  screenshotRel: string | null;
  manifest: Manifest | null;
  consoleIssues: string[];
}

const ISSUE_RE = /\[(ERROR|WARN)\]|TypeError|ReferenceError|Unhandled|failed/i;

function usage(): never {
  console.error("Usage: bunx tsx scripts/ui-workbench-report.ts <workbench-run-dir>");
  process.exit(2);
}

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function readJson<T>(filePath: string): T | null {
  try {
    return JSON.parse(readFileSync(filePath, "utf8")) as T;
  } catch {
    return null;
  }
}

function findScreenshot(dirPath: string): string | null {
  const file = readdirSync(dirPath)
    .filter((name) => name.endsWith(".png"))
    .sort()
    .find((name) => !name.includes("-diff"));
  return file ?? null;
}

function findConsoleIssues(filePath: string): string[] {
  if (!existsSync(filePath)) return [];
  return readFileSync(filePath, "utf8")
    .split("\n")
    .filter((line) => ISSUE_RE.test(line))
    .slice(0, 6);
}

function captureSortKey(name: string): string {
  const order = ["timeline-desktop", "timeline-mobile", "session-detail-desktop", "session-detail-mobile"];
  const index = order.indexOf(name);
  return `${index === -1 ? 99 : index}-${name}`;
}

function readCaptures(runDir: string): CaptureCard[] {
  return readdirSync(runDir)
    .filter((name) => {
      const fullPath = path.join(runDir, name);
      return statSync(fullPath).isDirectory();
    })
    .sort((a, b) => captureSortKey(a).localeCompare(captureSortKey(b)))
    .map((name) => {
      const dirPath = path.join(runDir, name);
      const manifest = readJson<Manifest>(path.join(dirPath, "manifest.json"));
      const screenshot = findScreenshot(dirPath);
      return {
        name,
        relDir: name,
        screenshotRel: screenshot ? `${name}/${screenshot}` : null,
        manifest,
        consoleIssues: findConsoleIssues(path.join(dirPath, "console.log")),
      };
    });
}

function renderStatus(card: CaptureCard): string {
  const errors = card.manifest?.errors ?? [];
  const issueCount = errors.length + card.consoleIssues.length + (card.screenshotRel ? 0 : 1);
  return issueCount === 0 ? "ok" : `${issueCount} issue${issueCount === 1 ? "" : "s"}`;
}

function renderIssues(card: CaptureCard): string {
  const errors = card.manifest?.errors ?? [];
  const issues = [
    ...errors.map((error) => `manifest: ${error}`),
    ...card.consoleIssues.map((line) => `console: ${line}`),
    ...(card.screenshotRel ? [] : ["missing screenshot"]),
  ];

  if (issues.length === 0) {
    return '<p class="muted">No manifest or console issues found.</p>';
  }

  return `<ul>${issues.map((issue) => `<li>${escapeHtml(issue)}</li>`).join("")}</ul>`;
}

function renderCard(card: CaptureCard): string {
  const manifest = card.manifest;
  const viewport = manifest?.config?.viewport;
  const viewportLabel = [
    manifest?.config?.viewport_name,
    viewport?.width && viewport?.height ? `${viewport.width}x${viewport.height}` : null,
    viewport?.deviceScaleFactor ? `@${viewport.deviceScaleFactor}x` : null,
  ].filter(Boolean).join(" ");
  const status = renderStatus(card);
  const statusClass = status === "ok" ? "status status-ok" : "status status-bad";

  return `
    <article class="card">
      <header>
        <div>
          <h2>${escapeHtml(card.name)}</h2>
          <p>${escapeHtml(manifest?.scene ?? "unknown scene")} · ${escapeHtml(viewportLabel || "unknown viewport")}</p>
        </div>
        <span class="${statusClass}">${escapeHtml(status)}</span>
      </header>
      ${
        card.screenshotRel
          ? `<a href="${escapeHtml(card.screenshotRel)}"><img src="${escapeHtml(card.screenshotRel)}" alt="${escapeHtml(card.name)} screenshot"></a>`
          : '<div class="missing">No screenshot</div>'
      }
      <details>
        <summary>Checks</summary>
        ${renderIssues(card)}
      </details>
    </article>
  `;
}

function renderHtml(runDir: string, cards: CaptureCard[]): string {
  const firstManifest = cards.find((card) => card.manifest)?.manifest;
  const git = firstManifest?.git;
  const title = path.basename(runDir);
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(title)}</title>
  <style>
    :root { color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #100b08; color: #f3ead9; }
    body { margin: 0; padding: 24px; background: radial-gradient(circle at 50% 0, rgba(56, 189, 248, 0.14), transparent 34%), #100b08; }
    main { max-width: 1600px; margin: 0 auto; display: flex; flex-direction: column; gap: 18px; }
    .top { display: flex; justify-content: space-between; gap: 16px; align-items: baseline; flex-wrap: wrap; }
    h1, h2, p { margin: 0; }
    h1 { font-size: 22px; }
    .meta, .card p, .muted { color: #b5a48e; font-size: 13px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(420px, 100%), 1fr)); gap: 16px; align-items: start; }
    .card { border: 1px solid rgba(212, 184, 122, 0.22); border-radius: 12px; background: rgba(18, 11, 9, 0.72); overflow: hidden; box-shadow: 0 18px 48px rgba(0,0,0,0.28); }
    .card header { display: flex; justify-content: space-between; gap: 12px; padding: 12px 14px; border-bottom: 1px solid rgba(212, 184, 122, 0.16); }
    .card h2 { font-size: 15px; }
    .status { align-self: flex-start; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }
    .status-ok { color: #d9f99d; background: rgba(34, 197, 94, 0.16); border: 1px solid rgba(134, 239, 172, 0.38); }
    .status-bad { color: #fecaca; background: rgba(239, 68, 68, 0.16); border: 1px solid rgba(248, 113, 113, 0.38); }
    img { display: block; width: 100%; height: auto; background: #0b0705; }
    details { padding: 10px 14px 12px; border-top: 1px solid rgba(212, 184, 122, 0.12); }
    summary { cursor: pointer; color: #d4b87a; font-size: 13px; }
    ul { margin: 8px 0 0; padding-left: 18px; color: #fca5a5; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.5; }
    .missing { padding: 32px; color: #fca5a5; }
  </style>
</head>
<body>
  <main>
    <div class="top">
      <div>
        <h1>${escapeHtml(title)}</h1>
        <p class="meta">Generated ${escapeHtml(new Date().toISOString())}</p>
      </div>
      <p class="meta">${escapeHtml(git?.branch ?? "unknown branch")} · ${escapeHtml(git?.sha ?? "unknown sha")}${git?.dirty ? " · dirty" : ""}</p>
    </div>
    <section class="grid">
      ${cards.map(renderCard).join("\n")}
    </section>
  </main>
</body>
</html>
`;
}

const runDir = process.argv[2] ? path.resolve(process.argv[2]) : usage();
if (!existsSync(runDir) || !statSync(runDir).isDirectory()) {
  console.error(`Not a directory: ${runDir}`);
  process.exit(1);
}

const cards = readCaptures(runDir);
if (cards.length === 0) {
  console.error(`No capture directories found in ${runDir}`);
  process.exit(1);
}

const outputPath = path.join(runDir, "index.html");
writeFileSync(outputPath, renderHtml(runDir, cards));
console.log(outputPath);
