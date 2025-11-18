import { describe, it, expect, beforeEach, vi, type Mock } from 'vitest';
import { ConversationController } from '../lib/conversation-controller';

describe('ConversationController', () => {
  let controller: ConversationController;
  let mockSessionManager: any;
  let mockRenderer: any;
  let onConversationIdChange: Mock;
  let onStreamingStart: Mock;
  let onStreamingComplete: Mock;

  beforeEach(() => {
    onConversationIdChange = vi.fn();
    onStreamingStart = vi.fn();
    onStreamingComplete = vi.fn();

    controller = new ConversationController({
      onConversationIdChange,
      onStreamingStart,
      onStreamingComplete
    });

    mockSessionManager = {
      addConversationTurn: vi.fn().mockResolvedValue(undefined),
      getConversationHistory: vi.fn().mockResolvedValue([])
    };

    mockRenderer = {
      addMessage: vi.fn(),
      updateMessage: vi.fn(),
      clear: vi.fn(),
      setStatus: vi.fn(),
      loadFromHistory: vi.fn()
    };

    controller.setSessionManager(mockSessionManager);
    controller.setRenderer(mockRenderer);
  });

  describe('Setup', () => {
    it('should initialize with null conversation ID', () => {
      expect(controller.getConversationId()).toBeNull();
    });

    it('should set conversation ID', () => {
      controller.setConversationId('test-id');
      expect(controller.getConversationId()).toBe('test-id');
      expect(onConversationIdChange).toHaveBeenCalledWith('test-id');
    });
  });

  describe('Turn Management', () => {
    it('should add user turn to UI', async () => {
      await controller.addUserTurn('Hello');

      expect(mockRenderer.addMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          role: 'user',
          content: 'Hello'
        })
      );
    });

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

    it('should add assistant turn to UI', async () => {
      await controller.addAssistantTurn('Hi there');

      expect(mockRenderer.addMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          role: 'assistant',
          content: 'Hi there'
        })
      );
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

      expect(mockRenderer.addMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          role: 'assistant',
          content: '',
          isStreaming: true
        })
      );
      expect(onStreamingStart).toHaveBeenCalled();
      expect(controller.isStreaming()).toBe(true);
    });

    it('should append streaming text', () => {
      controller.startStreaming();
      controller.appendStreaming('Hello');
      controller.appendStreaming(' world');

      expect(controller.getStreamingText()).toBe('Hello world');
      expect(mockRenderer.updateMessage).toHaveBeenCalledTimes(2);
    });

    it('should auto-start streaming on first append', () => {
      controller.appendStreaming('First chunk');

      expect(controller.isStreaming()).toBe(true);
      expect(mockRenderer.addMessage).toHaveBeenCalled();
    });

    it('should finalize streaming response', async () => {
      controller.setConversationId('conv-123');
      controller.startStreaming();
      controller.appendStreaming('Complete message');
      await controller.finalizeStreaming();

      expect(mockRenderer.updateMessage).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({
          isStreaming: false
        })
      );
      expect(mockSessionManager.addConversationTurn).toHaveBeenCalled();
      expect(onStreamingComplete).toHaveBeenCalled();
      expect(controller.isStreaming()).toBe(false);
    });

    it('should clear streaming state after finalize', async () => {
      controller.startStreaming();
      controller.appendStreaming('Text');
      await controller.finalizeStreaming();

      expect(controller.getStreamingText()).toBe('');
      expect(controller.isStreaming()).toBe(false);
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
    it('should load history from IndexedDB', async () => {
      const mockHistory = [
        { id: '1', userTranscript: 'Hello', timestamp: new Date() },
        { id: '2', assistantResponse: 'Hi', timestamp: new Date() }
      ];
      mockSessionManager.getConversationHistory.mockResolvedValue(mockHistory);
      controller.setConversationId('conv-123');

      await controller.loadHistory();

      expect(mockRenderer.loadFromHistory).toHaveBeenCalledWith(mockHistory);
    });

    it('should show placeholder when no history', async () => {
      mockSessionManager.getConversationHistory.mockResolvedValue([]);
      controller.setConversationId('conv-123');

      await controller.loadHistory();

      expect(mockRenderer.clear).toHaveBeenCalled();
      expect(mockRenderer.setStatus).toHaveBeenCalledWith(
        'No messages yet - tap the microphone to start',
        true
      );
    });

    it('should handle history load failure', async () => {
      mockSessionManager.getConversationHistory.mockRejectedValue(new Error('DB error'));
      controller.setConversationId('conv-123');

      await controller.loadHistory();

      expect(mockRenderer.setStatus).toHaveBeenCalledWith(
        'Failed to load conversation history',
        true
      );
    });

    it('should not load history without conversation ID', async () => {
      await controller.loadHistory();

      expect(mockSessionManager.getConversationHistory).not.toHaveBeenCalled();
      expect(mockRenderer.setStatus).toHaveBeenCalledWith(
        'Tap the microphone to start',
        true
      );
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
    it('should clear conversation', () => {
      controller.startStreaming();
      controller.appendStreaming('Text');
      controller.clear();

      expect(mockRenderer.clear).toHaveBeenCalled();
      expect(controller.isStreaming()).toBe(false);
      expect(controller.getStreamingText()).toBe('');
    });

    it('should set status message', () => {
      controller.setStatus('Connecting...', true);

      expect(mockRenderer.setStatus).toHaveBeenCalledWith('Connecting...', true);
    });
  });

  describe('Edge Cases', () => {
    it('should handle operations without renderer', async () => {
      controller.setRenderer(null);

      await expect(controller.addUserTurn('Hello')).resolves.not.toThrow();
      controller.startStreaming();
      controller.appendStreaming('Text');
      await expect(controller.finalizeStreaming()).resolves.not.toThrow();
    });

    it('should handle operations without session manager', async () => {
      controller.setSessionManager(null);

      await expect(controller.addUserTurn('Hello')).resolves.not.toThrow();
      await expect(controller.loadHistory()).resolves.not.toThrow();
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

      expect(mockRenderer.clear).toHaveBeenCalled();
      expect(controller.isStreaming()).toBe(false);
    });
  });
});
