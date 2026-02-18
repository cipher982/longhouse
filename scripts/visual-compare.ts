#!/usr/bin/env bun
/**
 * Visual Compare — hybrid pixelmatch + LLM triage for screenshot comparison.
 *
 * Two modes:
 *   bun run scripts/visual-compare.ts before.png after.png [--json] [--skip-llm]
 *   bun run scripts/visual-compare.ts --baseline-dir <dir> --current-dir <dir> [--json] [--skip-llm]
 *
 * Deps: pixelmatch, pngjs from apps/zerg/e2e/node_modules; @google/genai for LLM triage.
 * Run `bun install` in apps/zerg/e2e/ first.
 *
 * Exit codes: 0 = pass, 1 = failures detected, 2 = error
 */

import fs from "fs";
import path from "path";
import { PNG } from "pngjs";
import pixelmatch from "pixelmatch";

// --- Types ---

interface TriageResult {
  is_problem: boolean;
  severity: "critical" | "major" | "cosmetic" | "none";
  explanation: string;
}

interface PageResult {
  name: string;
  diff_ratio: number;
  pixel_diff_count: number;
  total_pixels: number;
  verdict: "pass" | "fail";
  llm_triage: TriageResult | null;
  diff_image_path: string | null;
}

interface Report {
  summary: "pass" | "fail";
  pages: PageResult[];
  timing: { pixelmatch_ms: number; llm_ms: number };
}

// --- Config ---

const PIXELMATCH_THRESHOLD = 0.05; // color sensitivity
const DIFF_PASS_LIMIT = 0.005; // below this = auto-pass (no LLM)
const DIFF_HARD_FAIL = 0.10; // above this = auto-fail (skip LLM)
const DIFF_FALLBACK_FAIL = 0.05; // when no API key, fail above this

// --- Pixelmatch ---

function compareImages(
  beforePath: string,
  afterPath: string,
  outputDir: string,
  name: string,
): { diffRatio: number; diffCount: number; totalPixels: number; diffImagePath: string | null } {
  const beforeBuf = fs.readFileSync(beforePath);
  const afterBuf = fs.readFileSync(afterPath);
  const before = PNG.sync.read(beforeBuf);
  const after = PNG.sync.read(afterBuf);

  // Size mismatch = auto-fail
  if (before.width !== after.width || before.height !== after.height) {
    return {
      diffRatio: 1.0,
      diffCount: before.width * before.height,
      totalPixels: before.width * before.height,
      diffImagePath: null,
    };
  }

  const { width, height } = before;
  const diff = new PNG({ width, height });
  const diffCount = pixelmatch(before.data, after.data, diff.data, width, height, {
    threshold: PIXELMATCH_THRESHOLD,
  });
  const totalPixels = width * height;
  const diffRatio = diffCount / totalPixels;

  let diffImagePath: string | null = null;
  if (diffRatio > 0.001) {
    fs.mkdirSync(outputDir, { recursive: true });
    diffImagePath = path.join(outputDir, `${name}-diff.png`);
    fs.writeFileSync(diffImagePath, PNG.sync.write(diff));
  }

  return { diffRatio, diffCount, totalPixels, diffImagePath };
}

// --- LLM Triage ---

const TRIAGE_PROMPT = `You are a visual QA expert reviewing UI screenshot differences.

You are given images:
1. BEFORE: The baseline screenshot
2. AFTER: The current screenshot
3. DIFF: A pixel-diff overlay (red pixels = changes) — only present if there is a diff image.

The pixelmatch diff ratio is {diff_ratio} ({diff_count} pixels changed out of {total_pixels}).

Analyze whether this difference represents a REAL PROBLEM or ACCEPTABLE VARIANCE.

REAL PROBLEMS (is_problem=true):
- Color catastrophes (backgrounds became opaque, colors completely wrong, alpha clamping)
- Broken layout (elements overlapping, missing sections, content overflow)
- Missing UI elements (buttons, cards, navigation gone)
- Text truncation or overflow
- Z-index issues (elements hidden behind others)

ACCEPTABLE VARIANCE (is_problem=false):
- Sub-pixel font rendering differences
- Minor anti-aliasing differences
- Slight shadow/gradient rendering variations
- 1-2px element position shifts from font metrics

Return ONLY this JSON object:
{
  "is_problem": true or false,
  "severity": "critical" or "major" or "cosmetic" or "none",
  "explanation": "One sentence explaining what changed and why it is/isn't a problem."
}`;

async function llmTriage(
  beforePath: string,
  afterPath: string,
  diffPath: string | null,
  diffRatio: number,
  diffCount: number,
  totalPixels: number,
): Promise<TriageResult> {
  const apiKey = process.env.GOOGLE_API_KEY;
  if (!apiKey) {
    // Fallback: hard threshold
    const isFail = diffRatio > DIFF_FALLBACK_FAIL;
    return {
      is_problem: isFail,
      severity: isFail ? "major" : "none",
      explanation: `LLM triage skipped (no GOOGLE_API_KEY). Diff ratio ${(diffRatio * 100).toFixed(2)}% ${isFail ? "exceeds" : "within"} fallback threshold.`,
    };
  }

  const { GoogleGenAI } = await import("@google/genai");
  const ai = new GoogleGenAI({ apiKey });

  const parts: Array<Record<string, unknown>> = [
    {
      inlineData: {
        mimeType: "image/png",
        data: fs.readFileSync(beforePath).toString("base64"),
      },
    },
    {
      inlineData: {
        mimeType: "image/png",
        data: fs.readFileSync(afterPath).toString("base64"),
      },
    },
  ];

  if (diffPath && fs.existsSync(diffPath)) {
    parts.push({
      inlineData: {
        mimeType: "image/png",
        data: fs.readFileSync(diffPath).toString("base64"),
      },
    });
  }

  const prompt = TRIAGE_PROMPT
    .replace("{diff_ratio}", (diffRatio * 100).toFixed(2) + "%")
    .replace("{diff_count}", String(diffCount))
    .replace("{total_pixels}", String(totalPixels));

  parts.push({ text: prompt });

  try {
    const response = await ai.models.generateContent({
      model: "gemini-2.0-flash",
      contents: [{ role: "user", parts }],
      config: {
        responseMimeType: "application/json",
      },
    });

    const text = response.text ?? "";
    return JSON.parse(text) as TriageResult;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return {
      is_problem: diffRatio > DIFF_FALLBACK_FAIL,
      severity: diffRatio > DIFF_FALLBACK_FAIL ? "major" : "none",
      explanation: `LLM triage error: ${msg}. Falling back to threshold.`,
    };
  }
}

// --- CLI ---

function usage() {
  console.error(`Usage:
  bun run scripts/visual-compare.ts <before.png> <after.png> [options]
  bun run scripts/visual-compare.ts --baseline-dir <dir> --current-dir <dir> [options]

Options:
  --json        Output JSON to stdout
  --skip-llm    Skip LLM triage, pixelmatch only
  --output-dir  Where to write diff images (default: ./visual-compare-results)
  --pages       Comma-separated page names (baseline-dir mode)`);
  process.exit(2);
}

async function comparePair(
  name: string,
  beforePath: string,
  afterPath: string,
  outputDir: string,
  skipLlm: boolean,
): Promise<PageResult> {
  const { diffRatio, diffCount, totalPixels, diffImagePath } = compareImages(
    beforePath,
    afterPath,
    outputDir,
    name,
  );

  // Auto-pass: diff below threshold
  if (diffRatio < DIFF_PASS_LIMIT) {
    return {
      name,
      diff_ratio: diffRatio,
      pixel_diff_count: diffCount,
      total_pixels: totalPixels,
      verdict: "pass",
      llm_triage: null,
      diff_image_path: diffImagePath,
    };
  }

  // Auto-fail: obviously broken
  if (diffRatio > DIFF_HARD_FAIL) {
    return {
      name,
      diff_ratio: diffRatio,
      pixel_diff_count: diffCount,
      total_pixels: totalPixels,
      verdict: "fail",
      llm_triage: {
        is_problem: true,
        severity: "critical",
        explanation: `Diff ratio ${(diffRatio * 100).toFixed(1)}% exceeds hard fail threshold (${DIFF_HARD_FAIL * 100}%).`,
      },
      diff_image_path: diffImagePath,
    };
  }

  // Middle zone: LLM triage
  if (skipLlm) {
    const isFail = diffRatio > DIFF_FALLBACK_FAIL;
    return {
      name,
      diff_ratio: diffRatio,
      pixel_diff_count: diffCount,
      total_pixels: totalPixels,
      verdict: isFail ? "fail" : "pass",
      llm_triage: null,
      diff_image_path: diffImagePath,
    };
  }

  const triage = await llmTriage(beforePath, afterPath, diffImagePath, diffRatio, diffCount, totalPixels);
  return {
    name,
    diff_ratio: diffRatio,
    pixel_diff_count: diffCount,
    total_pixels: totalPixels,
    verdict: triage.is_problem ? "fail" : "pass",
    llm_triage: triage,
    diff_image_path: diffImagePath,
  };
}

async function main() {
  const args = process.argv.slice(2);
  const jsonMode = args.includes("--json");
  const skipLlm = args.includes("--skip-llm") || process.env.SKIP_LLM === "1";

  const outputDirIdx = args.indexOf("--output-dir");
  const outputDir = outputDirIdx >= 0 ? args[outputDirIdx + 1] : "./visual-compare-results";

  const baselineDirIdx = args.indexOf("--baseline-dir");
  const currentDirIdx = args.indexOf("--current-dir");
  const pagesIdx = args.indexOf("--pages");

  const pmStart = Date.now();
  const results: PageResult[] = [];

  if (baselineDirIdx >= 0 && currentDirIdx >= 0) {
    // Baseline-dir mode
    const baselineDir = args[baselineDirIdx + 1];
    const currentDir = args[currentDirIdx + 1];
    const pageFilter = pagesIdx >= 0 ? args[pagesIdx + 1].split(",") : null;

    if (!fs.existsSync(baselineDir)) {
      console.error(`Baseline dir not found: ${baselineDir}`);
      process.exit(2);
    }
    if (!fs.existsSync(currentDir)) {
      console.error(`Current dir not found: ${currentDir}`);
      process.exit(2);
    }

    const baselineFiles = fs.readdirSync(baselineDir).filter((f) => f.endsWith(".png"));
    for (const file of baselineFiles) {
      const name = file.replace(/-chromium-darwin\.png$/, "").replace(/\.png$/, "");
      if (pageFilter && !pageFilter.includes(name)) continue;

      const currentFile = path.join(currentDir, file);
      if (!fs.existsSync(currentFile)) {
        // Try without platform suffix
        const altFile = path.join(currentDir, `${name}.png`);
        if (!fs.existsSync(altFile)) {
          results.push({
            name,
            diff_ratio: 1.0,
            pixel_diff_count: 0,
            total_pixels: 0,
            verdict: "fail",
            llm_triage: { is_problem: true, severity: "critical", explanation: "Current screenshot missing." },
            diff_image_path: null,
          });
          continue;
        }
        results.push(await comparePair(name, path.join(baselineDir, file), altFile, outputDir, skipLlm));
        continue;
      }
      results.push(await comparePair(name, path.join(baselineDir, file), currentFile, outputDir, skipLlm));
    }
  } else {
    // Single-pair mode
    const positional = args.filter((a) => !a.startsWith("--"));
    if (positional.length < 2) usage();
    const [beforePath, afterPath] = positional;
    if (!fs.existsSync(beforePath)) {
      console.error(`File not found: ${beforePath}`);
      process.exit(2);
    }
    if (!fs.existsSync(afterPath)) {
      console.error(`File not found: ${afterPath}`);
      process.exit(2);
    }
    const name = path.basename(beforePath, path.extname(beforePath));
    results.push(await comparePair(name, beforePath, afterPath, outputDir, skipLlm));
  }

  const pmEnd = Date.now();
  const llmStart = pmEnd;
  // LLM timing is embedded in comparePair calls above; approximate it
  const llmEnd = Date.now();

  const failures = results.filter((r) => r.verdict === "fail");
  const report: Report = {
    summary: failures.length > 0 ? "fail" : "pass",
    pages: results,
    timing: { pixelmatch_ms: pmEnd - pmStart, llm_ms: llmEnd - llmStart },
  };

  if (jsonMode) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    // Human-readable output
    const passCount = results.filter((r) => r.verdict === "pass").length;
    const failCount = failures.length;
    const triaged = results.filter((r) => r.llm_triage !== null).length;

    console.log(
      `Visual compare: ${passCount} pass, ${failCount} fail` +
        (triaged > 0 ? ` (${triaged} LLM-triaged)` : "") +
        ` (${report.timing.pixelmatch_ms}ms)`,
    );

    for (const f of failures) {
      console.log(`  FAIL: ${f.name} (${(f.diff_ratio * 100).toFixed(2)}% diff)`);
      if (f.llm_triage) {
        console.log(`        ${f.llm_triage.severity}: ${f.llm_triage.explanation}`);
      }
      if (f.diff_image_path) {
        console.log(`        diff: ${f.diff_image_path}`);
      }
    }
  }

  // Write report to output dir
  fs.mkdirSync(outputDir, { recursive: true });
  fs.writeFileSync(path.join(outputDir, "report.json"), JSON.stringify(report, null, 2));

  process.exit(failures.length > 0 ? 1 : 0);
}

main().catch((err) => {
  console.error(err);
  process.exit(2);
});
