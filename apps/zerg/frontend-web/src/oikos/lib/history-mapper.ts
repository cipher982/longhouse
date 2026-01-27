/**
 * History Mapper
 * Maps IndexedDB ConversationTurn records to OpenAI Realtime RealtimeMessageItem format
 *
 * This enables conversation history hydration after page refresh, so the
 * OpenAI Realtime session maintains context from previous interactions.
 */

import type { ConversationTurn } from '../data';
import type { RealtimeMessageItem } from '@openai/agents/realtime';

/**
 * Generate a short ID suitable for OpenAI Realtime API (max 32 chars).
 * Uses base36 encoding of timestamp + random suffix for uniqueness.
 */
function generateShortId(): string {
  const timestamp = Date.now().toString(36);
  const random = Math.random().toString(36).slice(2, 10);
  return `${timestamp}_${random}`.slice(0, 32);
}

/**
 * Map IndexedDB conversation turns to OpenAI Realtime message items.
 *
 * Each ConversationTurn can produce 0-2 RealtimeMessageItems:
 * - 1 user message (if userTranscript exists)
 * - 1 assistant message (if assistantResponse/assistantText exists)
 *
 * Items are sorted by timestamp and linked via previousItemId for continuity.
 *
 * @param turns - Conversation turns from IndexedDB
 * @returns Array of RealtimeMessageItem suitable for session.updateHistory()
 */
export function mapConversationToRealtimeItems(
  turns: ConversationTurn[]
): RealtimeMessageItem[] {
  // Sort by timestamp ascending to ensure correct order
  const sorted = [...turns].sort(
    (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
  );

  const items: RealtimeMessageItem[] = [];
  let previousItemId: string | null = null;

  for (const turn of sorted) {
    // Handle user message
    if (turn.userTranscript?.trim()) {
      const itemId = generateShortId();
      items.push({
        type: 'message',
        role: 'user',
        itemId,
        previousItemId,
        status: 'completed',
        content: [{ type: 'input_text', text: turn.userTranscript }],
      });
      previousItemId = itemId;
    }

    // Handle assistant message (normalize field names)
    const assistantText = turn.assistantResponse ?? turn.assistantText;
    if (assistantText?.trim()) {
      const itemId = generateShortId();
      items.push({
        type: 'message',
        role: 'assistant',
        itemId,
        previousItemId,
        status: 'completed',
        content: [{ type: 'output_text', text: assistantText }],
      });
      previousItemId = itemId;
    }
  }

  return items;
}

/**
 * Trim conversation history to the most recent N turns for Realtime API injection.
 *
 * This is separate from UI history limits (maxHistoryTurns) because:
 * - gpt-realtime has ~32k token context, with ~28k for input
 * - System instructions + tools already consume significant tokens
 * - 8-12 turns is a reasonable default for voice context
 *
 * @param turns - Full conversation history
 * @param maxTurns - Maximum number of turns to include (default: 8)
 * @returns Trimmed array of most recent turns
 */
export function trimForRealtime(
  turns: ConversationTurn[],
  maxTurns: number = 8
): ConversationTurn[] {
  if (turns.length <= maxTurns) {
    return turns;
  }

  // Sort by timestamp descending to get most recent, then take first N
  const sorted = [...turns].sort(
    (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
  );

  // Take most recent N, then reverse to get chronological order
  return sorted.slice(0, maxTurns).reverse();
}
