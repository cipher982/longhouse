import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

// The package root re-exports the full Prism build. Vite's dev server (the UI
// sweep serves the app from it) then loads every refractor language, about 300
// modules, the first time a page touches the highlighter: the New session
// frames took 2 to 4.5 s to open on crunch because of it. Import the light
// build and its language files by their own paths.
function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "__tests__" ? [] : sourceFiles(path);
    return /\.(ts|tsx)$/.test(name) ? [path] : [];
  });
}

describe("react-syntax-highlighter imports", () => {
  it("never import the package root", () => {
    const rootImport = /^\s*import\s+(?!type\b)[^;]*?from\s+["']react-syntax-highlighter["']/ms;
    const offenders = sourceFiles(resolve(__dirname, "../../..")).filter((file) =>
      rootImport.test(readFileSync(file, "utf8")),
    );
    expect(offenders).toEqual([]);
  });
});
