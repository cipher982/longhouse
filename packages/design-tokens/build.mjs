#!/usr/bin/env node

/**
 * Shared Design Token Builder
 *
 * Generates CSS and TypeScript outputs from tokens.json:
 * - dist/core.css       - All shared tokens
 * - dist/theme-solid.css - Solid surface tokens (dashboard)
 * - dist/theme-glass.css - Glass surface tokens (chat)
 * - dist/tokens.ts      - TypeScript exports
 */

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = __dirname;
const OUTPUT_DIR = path.join(ROOT, "dist");

const SPECIAL_KEYS = new Set(["$value", "$type", "$description", "$extensions", "$metadata", "$schema"]);

function toKebab(segment) {
  return segment
    .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
    .replace(/[_\s]+/g, "-")
    .toLowerCase();
}

function flattenTokens(node, trail = [], skipKeys = new Set()) {
  const entries = [];
  if (node && typeof node === "object" && !Array.isArray(node)) {
    const isToken = "$value" in node;

    if (isToken) {
      const { $value: value, $type: type = null } = node;
      entries.push({ path: trail, value, type });
      return entries;
    }

    for (const key of Object.keys(node)) {
      if (SPECIAL_KEYS.has(key) || skipKeys.has(key)) continue;
      entries.push(...flattenTokens(node[key], [...trail, key], skipKeys));
    }
  }
  return entries;
}

function toCssVarName(pathSegments) {
  return `--${pathSegments.map(toKebab).join("-")}`;
}

function formatCssValue(value, type) {
  if (typeof value === "string") {
    // Handle token references
    if (value.startsWith("{") && value.endsWith("}")) {
      const refPath = value.slice(1, -1).split(".").map(toKebab);
      return `var(--${refPath.join("-")})`;
    }
    return value;
  }

  if (Array.isArray(value)) {
    if (type === "cubicBezier" || type === "cubic-bezier") {
      return `cubic-bezier(${value.join(", ")})`;
    }
    return value.join(", ");
  }

  return String(value);
}

function tokenNodeToObject(node) {
  if (node && typeof node === "object" && !Array.isArray(node)) {
    if ("$value" in node) {
      return formatCssValue(node.$value, node.$type ?? null);
    }
    const result = {};
    for (const [key, value] of Object.entries(node)) {
      if (SPECIAL_KEYS.has(key)) continue;
      result[key] = tokenNodeToObject(value);
    }
    return result;
  }
  return node;
}

function tokensToJsObject(tokens) {
  const result = {};
  for (const [key, value] of Object.entries(tokens)) {
    if (key.startsWith("$")) continue;
    result[key] = tokenNodeToObject(value);
  }
  return result;
}

function generateCss(variables, layerName = "tokens") {
  const lines = [];
  lines.push("/* THIS FILE IS AUTO-GENERATED. DO NOT EDIT DIRECTLY. */");
  lines.push(`@layer ${layerName} {`);
  lines.push("  :root {");

  for (const token of variables) {
    lines.push(`    ${token.name}: ${token.value};`);
  }

  lines.push("  }");
  lines.push("}");
  lines.push("");

  return lines.join("\n");
}

async function build() {
  const tokensPath = path.join(ROOT, "tokens.json");
  const content = await readFile(tokensPath, "utf-8");
  const tokens = JSON.parse(content);

  await mkdir(OUTPUT_DIR, { recursive: true });

  // 1. Generate core.css - all tokens except theme-specific surfaces
  const coreTokens = { ...tokens };
  delete coreTokens.surface; // Surfaces are theme-specific

  const coreFlattened = flattenTokens(coreTokens);
  const coreVariables = coreFlattened
    .map(entry => ({
      name: toCssVarName(entry.path),
      value: formatCssValue(entry.value, entry.type),
    }))
    .sort((a, b) => a.name.localeCompare(b.name));

  const coreCss = generateCss(coreVariables, "tokens");
  await writeFile(path.join(OUTPUT_DIR, "core.css"), coreCss);
  console.log("✅ Wrote dist/core.css");

  // 2. Generate theme-solid.css - solid surface tokens (dashboard)
  const solidSurfaces = tokens.surface?.solid || {};
  const solidFlattened = flattenTokens({ surface: solidSurfaces }, ["color"]);
  const solidVariables = solidFlattened.map(entry => ({
    name: toCssVarName(entry.path),
    value: formatCssValue(entry.value, entry.type),
  }));

  // Add legacy aliases for dashboard compatibility
  const solidAliases = [
    { name: "--color-surface-page", value: "var(--color-surface-page)" },
    { name: "--color-surface-section", value: "var(--color-surface-section)" },
    { name: "--color-surface-card", value: "var(--color-surface-card)" },
    { name: "--color-surface-elevated", value: "var(--color-surface-elevated)" },
  ];

  // Map solid surfaces to generic surface names
  const solidMapped = [
    { name: "--color-surface-page", value: solidSurfaces.page?.$value || "#09090b" },
    { name: "--color-surface-section", value: solidSurfaces.section?.$value || "#18181b" },
    { name: "--color-surface-card", value: solidSurfaces.card?.$value || "#27272a" },
    { name: "--color-surface-elevated", value: solidSurfaces.elevated?.$value || "#3f3f46" },
    { name: "--color-surface-overlay", value: solidSurfaces.overlay?.$value || "rgba(250, 250, 250, 0.03)" },
  ];

  const solidCss = generateCss(solidMapped, "theme-solid");
  await writeFile(path.join(OUTPUT_DIR, "theme-solid.css"), solidCss);
  console.log("✅ Wrote dist/theme-solid.css");

  // 3. Generate theme-glass.css - glass surface tokens (chat)
  const glassSurfaces = tokens.surface?.glass || {};
  const glassMapped = [
    { name: "--color-void", value: glassSurfaces.void?.$value || "#030305" },
    { name: "--color-void-light", value: glassSurfaces.voidLight?.$value || "#0a0a12" },
    { name: "--color-surface-page", value: glassSurfaces.page?.$value || "#030305" },
    { name: "--color-surface-card", value: glassSurfaces.card?.$value || "rgba(255, 255, 255, 0.03)" },
    { name: "--color-surface-elevated", value: glassSurfaces.elevated?.$value || "rgba(255, 255, 255, 0.07)" },
    { name: "--color-surface-secondary", value: glassSurfaces.secondary?.$value || "rgba(10, 10, 15, 0.4)" },
    { name: "--color-glass", value: glassSurfaces.glass?.$value || "rgba(20, 20, 30, 0.6)" },
    { name: "--color-glass-border", value: glassSurfaces.glassBorder?.$value || "rgba(255, 255, 255, 0.08)" },
    { name: "--color-surface-dark", value: glassSurfaces.dark?.$value || "#1a1a2e" },
  ];

  const glassCss = generateCss(glassMapped, "theme-glass");
  await writeFile(path.join(OUTPUT_DIR, "theme-glass.css"), glassCss);
  console.log("✅ Wrote dist/theme-glass.css");

  // 4. Generate TypeScript exports
  const tokenObject = tokensToJsObject(tokens);
  const tsLines = [
    "// THIS FILE IS AUTO-GENERATED. DO NOT EDIT DIRECTLY.",
    "",
    "export const tokens = ",
    `${JSON.stringify(tokenObject, null, 2)} as const;`,
    "",
    "export type Tokens = typeof tokens;",
    "",
  ];

  await writeFile(path.join(OUTPUT_DIR, "tokens.ts"), tsLines.join("\n"));
  console.log("✅ Wrote dist/tokens.ts");

  // 5. Generate index.ts for easy imports
  const indexTs = [
    "// THIS FILE IS AUTO-GENERATED. DO NOT EDIT DIRECTLY.",
    "",
    "export * from './tokens';",
    "",
  ];
  await writeFile(path.join(OUTPUT_DIR, "index.ts"), indexTs.join("\n"));
  console.log("✅ Wrote dist/index.ts");

  console.log("\n🎨 Design tokens built successfully!");
}

build().catch(error => {
  console.error("Failed to build design tokens:", error);
  process.exit(1);
});
