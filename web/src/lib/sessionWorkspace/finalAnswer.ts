/**
 * Rendering for final-answer tool calls.
 *
 * A schema-constrained agent (a Claude subagent or workflow step) does not end
 * its turn with prose — it ends it by calling a return tool whose input IS the
 * answer, and whose result is a bare ack ("Structured output provided
 * successfully"). Rendered as an ordinary tool row that reads as a session
 * truncated mid-tool-call: an opaque chip with the internal tool name and no
 * payload, right where the deliverable should be.
 *
 * So we project the payload into the closing assistant message instead. Keys
 * are emitted in sorted order because Swift decodes JSON objects into an
 * unordered dictionary — sorting is the only ordering both clients can agree
 * on, and iOS/web parity is enforced by shared fixtures.
 */

type JsonRecord = Record<string, unknown>;

function isScalar(value: unknown): boolean {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean";
}

function scalarText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** Recursively key-sorted JSON, so web and iOS emit byte-identical blocks. */
function stableJson(value: unknown, indent: string): string {
  if (value === null || value === undefined) return "null";
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    const inner = indent + "  ";
    return "[\n" + value.map((entry) => inner + stableJson(entry, inner)).join(",\n") + "\n" + indent + "]";
  }
  if (typeof value === "object") {
    const keys = Object.keys(value as JsonRecord).sort();
    if (keys.length === 0) return "{}";
    const inner = indent + "  ";
    const body = keys
      .map((key) => `${inner}${JSON.stringify(key)}: ${stableJson((value as JsonRecord)[key], inner)}`)
      .join(",\n");
    return "{\n" + body + "\n" + indent + "}";
  }
  return JSON.stringify(value) ?? "null";
}

function fencedJson(value: unknown): string {
  return "```json\n" + stableJson(value, "") + "\n```";
}

function asRecord(input: unknown): JsonRecord | null {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  return input as JsonRecord;
}

/**
 * Markdown for a final-answer tool call's input, or null when there is nothing
 * to show and the caller should keep rendering an ordinary tool row.
 */
export function formatFinalAnswer(input: unknown): string | null {
  let value = input;
  if (typeof value === "string") {
    const text = value.trim();
    if (!text) return null;
    try {
      const parsed = JSON.parse(text) as unknown;
      if (asRecord(parsed)) value = parsed;
      else return text;
    } catch {
      return text;
    }
  }

  const record = asRecord(value);
  if (!record) {
    if (value == null) return null;
    return fencedJson(value);
  }

  const keys = Object.keys(record).sort();
  if (keys.length === 0) return null;

  // A single string field is the whole answer; its key is scaffolding.
  if (keys.length === 1 && typeof record[keys[0]] === "string") {
    const only = (record[keys[0]] as string).trim();
    return only || null;
  }

  const blocks: string[] = [];
  for (const key of keys) {
    const field = record[key];
    if (field == null) continue;
    if (Array.isArray(field) && field.length === 0) continue;

    let body: string;
    if (isScalar(field)) {
      body = scalarText(field).trim();
      if (!body) continue;
    } else if (Array.isArray(field) && field.every(isScalar)) {
      body = field.map((entry) => `- ${scalarText(entry).trim()}`).join("\n");
    } else {
      body = fencedJson(field);
    }
    blocks.push(`**${key}**\n\n${body}`);
  }

  return blocks.length > 0 ? blocks.join("\n\n") : null;
}
