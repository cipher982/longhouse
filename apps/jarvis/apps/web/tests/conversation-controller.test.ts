import { describe, it, expect, beforeEach, vi, type Mock } from 'vitest';
import { ConversationController } from '../lib/conversation-controller';

// Mock stateManager
vi.mock('../lib/state-manager', () => ({
  stateManager: {
    setStreamingText: vi.fn(),
    finalizeMessage: vi.fn(),
  }
}));

import { stateManager } from '../lib/state-manager';

describe('ConversationController', () => {
  let controller: ConversationController;
  let mockSessionManager: any;
  let listener: Mock;

  beforeEach(() => {
    vi.clearAllMocks();
    controller = new ConversationController();
    listener = vi.fn();
    controller.addListener(listener);

    mockSessionManager = {
      addConversationTurn: vi.fn().mockResolvedValue(undefined),
      getConversationHistory: vi.fn().mockResolvedValue([])
    };

    controller.setSessionManager(mockSessionManager);
  });

  describe('Setup', () => {
    it('should initialize with null conversation ID', () => {
      expect(controller.getConversationId()).toBeNull();
    });

    it('should set conversation ID', () => {
      controller.setConversationId('test-id');
      expect(controller.getConversationId()).toBe('test-id');

      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'conversationIdChange',
          id: 'test-id'
        })
      );
    });
  });

  describe('Turn Management', () => {
    it('should persist user turn to IndexedDB', async () => {
      controller.setConversationId('conv-123');
      await controller.addUserTurn('Hello');

      expect(mockSessionManager.addConversationTurn).toHaveBeenCalledWith(
        expect.objectContaining({
          userTranscript: 'Hello',
          conversationId: 'conv-123'
        })
      );
    });

    it('should not persist when timestamp provided (history load)', async () => {
      await controller.addUserTurn('Hello', new Date());

      expect(mockSessionManager.addConversationTurn).not.toHaveBeenCalled();
    });

    it('should persist assistant turn to IndexedDB', async () => {
      controller.setConversationId('conv-123');
      await controller.addAssistantTurn('Hi there');

      expect(mockSessionManager.addConversationTurn).toHaveBeenCalledWith(
        expect.objectContaining({
          assistantResponse: 'Hi there',
          conversationId: 'conv-123'
        })
      );
    });
  });

  describe('Streaming Management', () => {
    it('should start streaming response', () => {
      controller.startStreaming();

      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'streamingStart' })
      );
    });

    it('should append streaming text and notify stateManager', () => {
      controller.startStreaming();
      controller.appendStreaming('Hello');
      controller.appendStreaming(' world');

      expect(stateManager.setStreamingText).toHaveBeenCalledWith('Hello');
      expect(stateManager.setStreamingText).toHaveBeenCalledWith('Hello world');
    });

    it('should auto-start streaming on first append', () => {
      controller.appendStreaming('First chunk');

      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'streamingStart' })
      );
    });

    it('should finalize streaming response', async () => {
      controller.setConversationId('conv-123');
      controller.startStreaming();
      controller.appendStreaming('Complete message');

      listener.mockClear();
      await controller.finalizeStreaming();

      expect(mockSessionManager.addConversationTurn).toHaveBeenCalled();
      expect(stateManager.setStreamingText).toHaveBeenCalledWith('');
      expect(stateManager.finalizeMessage).toHaveBeenCalledWith('Complete message');

      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'streamingStop' })
      );
    });

    it('should handle multiple streaming sessions', async () => {
      // First streaming session
      controller.startStreaming();
      controller.appendStreaming('First');
      await controller.finalizeStreaming();

      // Second streaming session
      controller.startStreaming();
      controller.appendStreaming('Second');
      await controller.finalizeStreaming();

      expect(mockSessionManager.addConversationTurn).toHaveBeenCalledTimes(2);
    });
  });

  describe('History Management', () => {
    it('should return history from IndexedDB', async () => {
      const mockHistory = [
        { id: '1', userTranscript: 'Hello', timestamp: new Date() },
        { id: '2', assistantResponse: 'Hi', timestamp: new Date() }
      ];
      mockSessionManager.getConversationHistory.mockResolvedValue(mockHistory);
      controller.setConversationId('conv-123');

      const history = await controller.getHistory();

      expect(history).toEqual(mockHistory);
    });

    it('should return empty array when no conversation ID', async () => {
      const history = await controller.getHistory();

      expect(history).toEqual([]);
      expect(mockSessionManager.getConversationHistory).not.toHaveBeenCalled();
    });

    it('should return empty array on error', async () => {
      mockSessionManager.getConversationHistory.mockRejectedValue(new Error('DB error'));
      controller.setConversationId('conv-123');

      const history = await controller.getHistory();

      expect(history).toEqual([]);
    });
  });

  describe('Conversation Item Events', () => {
    it('should handle item added event', () => {
      const event = { type: 'conversation.item.added', item: {} };
      controller.handleItemAdded(event);
      // Just logs for now
    });

    it('should handle item done event', () => {
      const event = { type: 'conversation.item.done', item: {} };
      controller.handleItemDone(event);
      // Just logs for now
    });
  });

  describe('Utility Methods', () => {
    it('should clear streaming state', () => {
      controller.startStreaming();
      controller.appendStreaming('Text');
      controller.clear();

      expect(stateManager.setStreamingText).toHaveBeenCalledWith('');
    });
  });

  describe('Edge Cases', () => {
    it('should handle operations without session manager', async () => {
      controller.setSessionManager(null);

      await expect(controller.addUserTurn('Hello')).resolves.not.toThrow();
      const history = await controller.getHistory();
      expect(history).toEqual([]);
    });

    it('should handle empty streaming finalization', async () => {
      controller.startStreaming();
      await controller.finalizeStreaming();

      // Should not persist empty message
      expect(mockSessionManager.addConversationTurn).not.toHaveBeenCalled();
    });
  });

  describe('Cleanup', () => {
    it('should dispose resources', () => {
      controller.startStreaming();
      controller.setConversationId('conv-123');
      controller.dispose();

      expect(stateManager.setStreamingText).toHaveBeenCalledWith('');
    });
  });
});
