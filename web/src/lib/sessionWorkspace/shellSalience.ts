/**
 * Content-aware salience for shell tool calls (spec: Change B in
 * docs/specs/timeline-reading-experience.md).
 *
 * A conservative read-only allowlist demotes commands to the noise tier so
 * they can join exploration runs; everything the grammar does not
 * affirmatively recognize stays `null` (= keep the tool's static action
 * tier). Fail closed at every rule: the classifier detects boring, never
 * danger, so a miss renders a boring command full-size — the status quo —
 * while a mutation can only demote by passing an explicit read-only shape.
 *
 * The grammar is handwritten here and mirrored in
 * ios/Sources/Shared/ShellSalience.swift. Behavioral parity is enforced by
 * config/shell-salience-fixtures.json, which both test suites run in full.
 * Change the fixtures first, then both implementations.
 */

import {
  SHELL_AGGREGATE_BY_HEAD,
  SHELL_DEFAULT_READ_AGGREGATE,
  SHELL_GIT_READ_SUBCOMMANDS,
  SHELL_READ_ONLY_COMMANDS,
  SHELL_TOOLS,
  type ToolAggregate,
} from "./toolTiers.generated";

export interface ShellSalience {
  tier: "noise";
  aggregate: ToolAggregate;
}

export function isShellTool(toolName: string): boolean {
  return SHELL_TOOLS.has(toolName);
}

/**
 * Opaque-on-sight structures: anything that can write, execute, or that we
 * refuse to parse. Newlines, backgrounding `&`, `|&`, every write-redirect
 * spelling, heredocs/herestrings, process/command substitution, backticks,
 * control-flow keywords, subshells.
 */
const OPAQUE_STRUCTURE =
  /[\n\r`]|<<|<<<|\$\(|<\(|>\(|\|&|&>|>\||\d+>|>>|(?<!&)>(?!&)|(?<!&)&(?!&)|(^|[\s;(])(for|while|if|until|case|function)\s|\(\s*\)/;

function hasBalancedQuotes(command: string): boolean {
  let single = false;
  let double = false;
  for (let i = 0; i < command.length; i++) {
    const ch = command[i];
    if (ch === "\\" && !single) {
      i += 1;
      continue;
    }
    if (ch === "'" && !double) single = !single;
    else if (ch === '"' && !single) double = !double;
  }
  return !single && !double;
}

/** Strip leading VAR=value assignments from a segment. */
function stripAssignments(segment: string): string {
  let s = segment;
  for (;;) {
    const next = s.replace(/^[A-Za-z_][A-Za-z0-9_]*=[^\s]*\s+/, "");
    if (next === s) return s;
    s = next;
  }
}

/** `sed` is read only in an explicit print shape: -n, no in-place in any
 * spelling, and a print-only script (addresses + `p`, e.g. `-n '120,160p'`). */
function sedIsRead(parts: string[]): boolean {
  const args = parts.slice(1);
  if (args.some((p) => p === "-i" || p.startsWith("-i") || p.startsWith("--in-place"))) {
    return false;
  }
  if (!args.includes("-n")) return false;
  const script = args.find((p) => !p.startsWith("-"));
  if (!script) return false;
  const body = script.replace(/^['"]|['"]$/g, "");
  return /^[0-9,$;\s]*p$/.test(body);
}

/** `git` is read only for an allowlisted subcommand, skipping global options. */
function gitIsRead(parts: string[]): boolean {
  let i = 1;
  while (i < parts.length) {
    const p = parts[i];
    if (p === "-C" || p === "-c" || p === "--git-dir" || p === "--work-tree") {
      i += 2;
      continue;
    }
    if (p.startsWith("--git-dir=") || p.startsWith("--work-tree=") || p === "--no-pager") {
      i += 1;
      continue;
    }
    break;
  }
  return i < parts.length && SHELL_GIT_READ_SUBCOMMANDS.has(parts[i]);
}

/**
 * Classify a raw shell command. Returns the demoted salience for read-only
 * commands, or `null` for anything unrecognized (keep the static action
 * tier). The first read head word drives the aggregate.
 */
export function classifyShellCommand(command: unknown): ShellSalience | null {
  if (typeof command !== "string" || command.length === 0 || command.length > 4000) {
    return null;
  }
  if (OPAQUE_STRUCTURE.test(command)) return null;
  if (!hasBalancedQuotes(command)) return null;

  const segments = command.trim().split(/&&|\|\||;|\|/);
  let firstReadHead: string | null = null;

  for (const rawSegment of segments) {
    const segment = stripAssignments(rawSegment.trim());
    if (!segment) continue;
    const parts = segment.split(/\s+/);
    const head = parts[0];
    // Bare names only: a path like /tmp/ls is not the trusted ls.
    if (head.includes("/")) return null;
    if (head === "cd") continue;
    if (head === "sed") {
      if (!sedIsRead(parts)) return null;
      firstReadHead ??= head;
      continue;
    }
    if (head === "git") {
      if (!gitIsRead(parts)) return null;
      firstReadHead ??= head;
      continue;
    }
    if (!SHELL_READ_ONLY_COMMANDS.has(head)) return null;
    firstReadHead ??= head;
  }

  // A command that is only `cd`/assignments has no read meaning.
  if (firstReadHead == null) return null;

  return {
    tier: "noise",
    aggregate: SHELL_AGGREGATE_BY_HEAD[firstReadHead] ?? SHELL_DEFAULT_READ_AGGREGATE,
  };
}
