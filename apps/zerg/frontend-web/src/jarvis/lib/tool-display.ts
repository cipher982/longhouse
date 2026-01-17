const EXEC_TOOL_NAMES = new Set(['runner_exec', 'ssh_exec', 'container_exec']);
const EXEC_TARGET_KEYS = ['target', 'host', 'container', 'container_id', 'containerId'];

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

export function extractExecTarget(toolName: string, argsPreview?: string | null): string | null {
  if (!argsPreview) return null;
  const trimmed = argsPreview.trim();
  if (!trimmed) return null;

  const normalizedName = toolName.toLowerCase();
  const shouldSearch = EXEC_TOOL_NAMES.has(normalizedName) || EXEC_TARGET_KEYS.some(key => trimmed.includes(key));
  if (!shouldSearch) return null;

  const patterns = [
    /"target"\s*:\s*"([^"]+)"/,
    /'target'\s*:\s*'([^']+)'/,
    /target\s*=\s*"([^"]+)"/,
    /target\s*=\s*'([^']+)'/,
    /"host"\s*:\s*"([^"]+)"/,
    /'host'\s*:\s*'([^']+)'/,
    /host\s*=\s*"([^"]+)"/,
    /host\s*=\s*'([^']+)'/,
    /"container_id"\s*:\s*"([^"]+)"/,
    /'container_id'\s*:\s*'([^']+)'/,
    /container_id\s*=\s*"([^"]+)"/,
    /container_id\s*=\s*'([^']+)'/,
    /"container"\s*:\s*"([^"]+)"/,
    /'container'\s*:\s*'([^']+)'/,
    /container\s*=\s*"([^"]+)"/,
    /container\s*=\s*'([^']+)'/,
  ];

  for (const pattern of patterns) {
    const match = trimmed.match(pattern);
    if (match && match[1]) {
      return match[1];
    }
  }

  return null;
}

export function extractExitCode(resultPreview?: string | null, error?: string | null): number | null {
  const sources = [resultPreview, error].filter(Boolean) as string[];
  if (sources.length === 0) return null;

  const patterns = [
    /exit_code\s*[:=]\s*(\d+)/i,
    /"exit_code"\s*:\s*(\d+)/i,
    /'exit_code'\s*:\s*(\d+)/i,
    /exitCode\s*[:=]\s*(\d+)/i,
    /exit\s*status\s*[:=]?\s*(\d+)/i,
  ];

  for (const source of sources) {
    for (const pattern of patterns) {
      const match = source.match(pattern);
      if (match && match[1]) {
        const parsed = Number.parseInt(match[1], 10);
        if (!Number.isNaN(parsed)) {
          return parsed;
        }
      }
    }
  }

  return null;
}
