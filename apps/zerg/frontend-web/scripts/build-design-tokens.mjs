#!/usr/bin/env node

/**
 * Design Token Builder
 *
 * Reads DTCG-compliant tokens from design-tokens/tokens.json and emits a
 * generated CSS variable file at src/styles/generated/tokens.css. Legacy
 * variable aliases are re-created from design-tokens/aliases.json so that
 * existing global styles continue to function.
 */

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const TOKENS_DIR = path.join(ROOT, "design-tokens");
const SRC_STYLES_DIR = path.join(ROOT, "src", "styles");
const OUTPUT_DIR = path.join(SRC_STYLES_DIR, "generated");
const OUTPUT_FILE = path.join(OUTPUT_DIR, "tokens.css");
const OUTPUT_TS_FILE = path.join(OUTPUT_DIR, "tokens.ts");

const SPECIAL_KEYS = new Set(["$value", "$type", "$description", "$extensions", "$metadata"]);

/**
 * Convert camelCase/PascalCase/underscored segments to kebab-case.
 */
function toKebab(segment) {
  return segment
    .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
    .replace(/[_\s]+/g, "-")
    .toLowerCase();
}

if (process.env.DEBUG_TOKENS) {
  console.log("toKebab test:", toKebab("shadow"), toKebab("sm"));
}

/**
 * Flatten nested token objects into an array of { path, value, type } entries.
 */
function flattenTokens(node, trail = []) {
  const entries = [];
  if (node && typeof node === "object" && !Array.isArray(node)) {
    const keys = Object.keys(node);
    const isToken = "$value" in node;

    if (isToken) {
      const { $value: value, $type: type = null } = node;
      entries.push({ path: trail, value, type });
      return entries;
    }

    for (const key of keys) {
      if (SPECIAL_KEYS.has(key)) continue;
      const child = node[key];
      entries.push(...flattenTokens(child, [...trail, key]));
    }
  }
  return entries;
}

/**
 * Convert a token path array into a CSS custom property identifier.
 */
function toCssVarName(pathSegments) {
  return `--${pathSegments.map(toKebab).join("-")}`;
}

/**
 * Resolve token references such as `{font.size.base}` into CSS variable references.
 */
function resolveValue(rawValue) {
  if (typeof rawValue === "string" && rawValue.startsWith("{") && rawValue.endsWith("}")) {
    const refPath = rawValue.slice(1, -1).split(".").map(toKebab);
    return `var(--${refPath.join("-")})`;
  }
  return rawValue;
}

/**
 * Format the token value for CSS output depending on token type.
 */
function formatCssValue(value, type) {
  const resolved = resolveValue(value);

  if (typeof resolved === "string") {
    return resolved;
  }

  if (Array.isArray(resolved)) {
    if (type === "cubicBezier" || type === "cubic-bezier") {
      return `cubic-bezier(${resolved.join(", ")})`;
    }
    return resolved.join(", ");
  }

  // Numbers or other primitive types
  return String(resolved);
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

async function readJson(relativePath) {
  const absolute = path.join(TOKENS_DIR, relativePath);
  const content = await readFile(absolute, "utf-8");
  return JSON.parse(content);
}

async function build() {
  const tokens = await readJson("tokens.json");
  const aliases = await readJson("aliases.json");

  const flattened = flattenTokens(tokens);
  const definedPaths = new Set(flattened.map(entry => entry.path.join(".")));

  for (const entry of flattened) {
    if (typeof entry.value !== "string") continue;
    const matches = entry.value.match(/\{([^}]+)\}/g);
    if (!matches) continue;
    for (const rawRef of matches) {
      const ref = rawRef.slice(1, -1);
      if (!definedPaths.has(ref)) {
        throw new Error(`Undefined token reference "${ref}" in ${entry.path.join(".")}`);
      }
    }
  }

  const cssVariables = flattened
    .map(entry => {
      const cssVar = toCssVarName(entry.path);
      const cssValue = formatCssValue(entry.value, entry.type);
      return { name: cssVar, value: cssValue, path: entry.path };
    })
    // Sort alphabetically for stable output
    .sort((a, b) => a.name.localeCompare(b.name));

  if (process.env.DEBUG_TOKENS) {
    const preview = cssVariables.slice(0, 10);
    const debug = cssVariables.slice(0, 3).map(entry => ({
      path: entry.path,
      kebab: entry.path.map(toKebab),
      codes: entry.path.map(segment => Array.from(segment, ch => ch.charCodeAt(0)))
    }));
    console.log("Token preview:", preview);
    console.log("Paths:", preview.map(entry => entry.path));
    console.dir(debug, { depth: null });
  }

  const canonicalNames = new Set(cssVariables.map(token => token.name));

  const aliasEntries = Object.entries(aliases)
    .flatMap(([alias, target]) => {
      const targetPath = target.trim();
      if (!definedPaths.has(targetPath)) {
        throw new Error(`Alias target "${targetPath}" (for ${alias}) is not a defined token`);
      }
      const targetVarName = `--${targetPath.split(".").map(toKebab).join("-")}`;
      if (alias === targetVarName || canonicalNames.has(alias)) {
        return [];
      }
      const refVar = `var(${targetVarName})`;
      return [{ name: alias, value: refVar }];
    })
    .sort((a, b) => a.name.localeCompare(b.name));

  const tokenObject = tokensToJsObject(tokens);

  const lines = [];
  lines.push("/* THIS FILE IS AUTO-GENERATED. DO NOT EDIT DIRECTLY. */");
  lines.push("@layer tokens {");
  lines.push("  :root {");

  for (const token of cssVariables) {
    lines.push(`    ${token.name}: ${token.value};`);
  }

  if (aliasEntries.length > 0) {
    lines.push("");
    lines.push("    /* Legacy aliases */");
    for (const alias of aliasEntries) {
      lines.push(`    ${alias.name}: ${alias.value};`);
    }
  }

  lines.push("  }");
  lines.push("}");
  lines.push("");

  await mkdir(OUTPUT_DIR, { recursive: true });
  await writeFile(OUTPUT_FILE, lines.join("\n"));
  console.log(`✅ Wrote ${OUTPUT_FILE}`);

  const tsLines = [];
  tsLines.push("// THIS FILE IS AUTO-GENERATED. DO NOT EDIT DIRECTLY.");
  tsLines.push("export const tokens = ");
  tsLines.push(`${JSON.stringify(tokenObject, null, 2)} as const;`);
  tsLines.push("");
  tsLines.push("export type Tokens = typeof tokens;");
  tsLines.push("");

  await writeFile(OUTPUT_TS_FILE, tsLines.join("\n"));
  console.log(`✅ Wrote ${OUTPUT_TS_FILE}`);
}

build().catch(error => {
  console.error("Failed to build design tokens:", error);
  process.exit(1);
});
