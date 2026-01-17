const EXEC_TOOL_NAMES = new Set(['runner_exec', 'ssh_exec', 'container_exec']);

export function extractCommandPreview(toolName: string, argsPreview?: string | null): string | null {
  if (!argsPreview) return null;
  const trimmed = argsPreview.trim();
  if (!trimmed) return null;

  const normalizedName = toolName.toLowerCase();
  const shouldSearch = EXEC_TOOL_NAMES.has(normalizedName) || trimmed.includes('command');
  if (!shouldSearch) return null;

  const patterns = [
    /"command"\s*:\s*"([^"]+)"/,
    /'command'\s*:\s*'([^']+)'/,
    /command\s*=\s*"([^"]+)"/,
    /command\s*=\s*'([^']+)'/,
    /command\s*=\s*([^,}\s]+)/,
  ];

  for (const pattern of patterns) {
    const match = trimmed.match(pattern);
    if (match && match[1]) {
      return match[1];
    }
  }

  // If args preview is already a command string (no structured payload), use it.
  if ((normalizedName.endsWith('_exec') || normalizedName.includes('exec')) && !/[{}]/.test(trimmed) && trimmed.length <= 200) {
    return trimmed;
  }

  return null;
}
