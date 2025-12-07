/**
 * Tests for history-mapper.ts
 * Ensures ConversationTurn → RealtimeMessageItem mapping is correct
 */

import { describe, it, expect } from 'vitest';
import { mapConversationToRealtimeItems, trimForRealtime } from '../lib/history-mapper';
import type { ConversationTurn } from '@jarvis/data-local';

describe('history-mapper', () => {
  describe('mapConversationToRealtimeItems', () => {
    it('maps empty array to empty array', () => {
      const result = mapConversationToRealtimeItems([]);
      expect(result).toEqual([]);
    });

    it('maps user transcript to user message item', () => {
      const turns: ConversationTurn[] = [{
        id: '1',
        timestamp: new Date('2024-01-01T10:00:00Z'),
        userTranscript: 'Hello world'
      }];

      const result = mapConversationToRealtimeItems(turns);

      expect(result).toHaveLength(1);
      expect(result[0]).toMatchObject({
        type: 'message',
        role: 'user',
        status: 'completed',
        content: [{ type: 'input_text', text: 'Hello world' }]
      });
      expect(result[0].itemId).toBeDefined();
      expect(result[0].previousItemId).toBeNull();
    });

    it('maps assistant response to assistant message item', () => {
      const turns: ConversationTurn[] = [{
        id: '1',
        timestamp: new Date('2024-01-01T10:00:00Z'),
        assistantResponse: 'Hi there!'
      }];

      const result = mapConversationToRealtimeItems(turns);

      expect(result).toHaveLength(1);
      expect(result[0]).toMatchObject({
        type: 'message',
        role: 'assistant',
        status: 'completed',
        content: [{ type: 'output_text', text: 'Hi there!' }]
      });
    });

    it('handles assistantText as fallback for assistantResponse', () => {
      const turns: ConversationTurn[] = [{
        id: '1',
        timestamp: new Date('2024-01-01T10:00:00Z'),
        assistantText: 'Fallback text'
      }];

      const result = mapConversationToRealtimeItems(turns);

      expect(result).toHaveLength(1);
      expect(result[0].content[0]).toMatchObject({
        type: 'output_text',
        text: 'Fallback text'
      });
    });

    it('maps full conversation turn to user + assistant messages', () => {
      const turns: ConversationTurn[] = [{
        id: '1',
        timestamp: new Date('2024-01-01T10:00:00Z'),
        userTranscript: 'What is the codeword?',
        assistantResponse: 'The codeword is banana.'
      }];

      const result = mapConversationToRealtimeItems(turns);

      expect(result).toHaveLength(2);
      expect(result[0].role).toBe('user');
      expect(result[1].role).toBe('assistant');
      expect(result[1].previousItemId).toBe(result[0].itemId);
    });

    it('maintains previousItemId chain across multiple turns', () => {
      const turns: ConversationTurn[] = [
        {
          id: '1',
          timestamp: new Date('2024-01-01T10:00:00Z'),
          userTranscript: 'First question',
          assistantResponse: 'First answer'
        },
        {
          id: '2',
          timestamp: new Date('2024-01-01T10:01:00Z'),
          userTranscript: 'Second question',
          assistantResponse: 'Second answer'
        }
      ];

      const result = mapConversationToRealtimeItems(turns);

      expect(result).toHaveLength(4);
      expect(result[0].previousItemId).toBeNull(); // First user
      expect(result[1].previousItemId).toBe(result[0].itemId); // First assistant
      expect(result[2].previousItemId).toBe(result[1].itemId); // Second user
      expect(result[3].previousItemId).toBe(result[2].itemId); // Second assistant
    });

    it('sorts by timestamp ascending', () => {
      const turns: ConversationTurn[] = [
        {
          id: '2',
          timestamp: new Date('2024-01-01T10:01:00Z'),
          userTranscript: 'Second'
        },
        {
          id: '1',
          timestamp: new Date('2024-01-01T10:00:00Z'),
          userTranscript: 'First'
        }
      ];

      const result = mapConversationToRealtimeItems(turns);

      expect(result).toHaveLength(2);
      expect(result[0].content[0]).toMatchObject({ text: 'First' });
      expect(result[1].content[0]).toMatchObject({ text: 'Second' });
    });

    it('skips empty/whitespace transcripts', () => {
      const turns: ConversationTurn[] = [{
        id: '1',
        timestamp: new Date('2024-01-01T10:00:00Z'),
        userTranscript: '   ',
        assistantResponse: ''
      }];

      const result = mapConversationToRealtimeItems(turns);
      expect(result).toHaveLength(0);
    });
  });

  describe('trimForRealtime', () => {
    it('returns all turns when under limit', () => {
      const turns: ConversationTurn[] = [
        { id: '1', timestamp: new Date('2024-01-01T10:00:00Z'), userTranscript: 'One' },
        { id: '2', timestamp: new Date('2024-01-01T10:01:00Z'), userTranscript: 'Two' },
      ];

      const result = trimForRealtime(turns, 8);
      expect(result).toHaveLength(2);
    });

    it('trims to most recent N turns', () => {
      const turns: ConversationTurn[] = [
        { id: '1', timestamp: new Date('2024-01-01T10:00:00Z'), userTranscript: 'First' },
        { id: '2', timestamp: new Date('2024-01-01T10:01:00Z'), userTranscript: 'Second' },
        { id: '3', timestamp: new Date('2024-01-01T10:02:00Z'), userTranscript: 'Third' },
        { id: '4', timestamp: new Date('2024-01-01T10:03:00Z'), userTranscript: 'Fourth' },
      ];

      const result = trimForRealtime(turns, 2);

      expect(result).toHaveLength(2);
      // Should be last 2 in chronological order
      expect(result[0].userTranscript).toBe('Third');
      expect(result[1].userTranscript).toBe('Fourth');
    });

    it('handles default maxTurns of 8', () => {
      const turns: ConversationTurn[] = Array.from({ length: 15 }, (_, i) => ({
        id: String(i),
        timestamp: new Date(Date.now() + i * 1000),
        userTranscript: `Turn ${i}`
      }));

      const result = trimForRealtime(turns);
      expect(result).toHaveLength(8);
    });
  });
});
