/**
 * SSE helpers for E2E tests.
 *
 * Playwright's APIRequestContext waits for full response bodies, which never
 * complete for SSE streams. Use fetch + AbortController to read just enough
 * events and then abort.
 */

export type SseEvent = {
  event: string;
  data: unknown;
};

type PostSseOptions = {
  backendUrl: string;
  commisId: string;
  path: string;
  payload: unknown;
  timeoutMs?: number;
  stopEvent?: string;
  stopOnFirstEvent?: boolean;
};

function parseSseChunk(chunk: string): SseEvent | null {
  const lines = chunk.split('\n');
  let event = '';
  const dataLines: string[] = [];

  for (const line of lines) {
    if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trim());
    }
  }

  if (!event && dataLines.length === 0) {
    return null;
  }

  const raw = dataLines.join('\n').trim();
  if (!raw) {
    return { event: event || 'message', data: '' };
  }

  try {
    return { event: event || 'message', data: JSON.parse(raw) };
  } catch {
    return { event: event || 'message', data: raw };
  }
}

export async function postSseAndCollect(options: PostSseOptions): Promise<SseEvent[]> {
  const {
    backendUrl,
    commisId,
    path,
    payload,
    timeoutMs = 30000,
    stopEvent,
    stopOnFirstEvent = false,
  } = options;

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  const events: SseEvent[] = [];

  try {
    const response = await fetch(`${backendUrl}${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Test-Commis': commisId,
        Accept: 'text/event-stream',
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!response.ok) {
      const body = await response.text().catch(() => '');
      throw new Error(`SSE POST ${path} failed: ${response.status} ${body}`);
    }

    if (!response.body) {
      return events;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');

      let idx = buffer.indexOf('\n\n');
      while (idx >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const parsed = parseSseChunk(chunk);
        if (parsed) {
          events.push(parsed);
          if (stopEvent && parsed.event === stopEvent) {
            controller.abort();
            return events;
          }
          if (stopOnFirstEvent) {
            controller.abort();
            return events;
          }
        }
        idx = buffer.indexOf('\n\n');
      }
    }
  } catch (error) {
    if ((error as Error).name === 'AbortError') {
      return events;
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }

  return events;
}
