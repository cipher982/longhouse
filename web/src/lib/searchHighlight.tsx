// ---------------------------------------------------------------------------
// Search highlighting
// ---------------------------------------------------------------------------

export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function renderHighlightedText(text: string, query: string) {
  const tokens = query.split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return text;
  const pattern = tokens.map(escapeRegExp).join("|");
  if (!pattern) return text;
  const splitRegex = new RegExp(`(${pattern})`, "gi");
  const matchRegex = new RegExp(`^(${pattern})$`, "i");

  return text.split(splitRegex).map((part, idx) =>
    matchRegex.test(part) ? (
      <mark key={`${idx}-${part}`} className="search-highlight">
        {part}
      </mark>
    ) : (
      part
    ),
  );
}
