/**
 * SupervisorChatController - Manages text chat with Supervisor via Zerg backend
 *
 * Responsibilities:
 * - Send messages to POST /api/jarvis/chat (SSE streaming)
 * - Handle SSE stream for streaming responses
 * - Load conversation history from GET /api/jarvis/history
 * - Emit events for UI updates via stateManager
 *
 * Usage:
 *   const controller = new SupervisorChatController();
 *   await controller.initialize();
 *   await controller.sendMessage("Hello, assistant!");
 */

import { logger } from '@jarvis/core';
import { stateManager } from './state-manager';
import { conversationController } from './conversation-controller';
import { CONFIG, toAbsoluteUrl } from './config';
import { eventBus } from './event-bus';

export interface SupervisorChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;
}

export interface SupervisorChatConfig {
  maxRetries?: number;
  retryDelay?: number;
}

/**
 * SSE event types from /api/jarvis/chat
 */
interface SSEConnectedEvent {
  message: string;
  run_id: number;
}

interface SSESupervisorEvent {
  type: 'supervisor_started' | 'supervisor_thinking' | 'supervisor_complete' | 'error';
  payload: {
    run_id?: number;
    message?: string;
    result?: string;
    error?: string;
  };
}

export class SupervisorChatController {
  private config: SupervisorChatConfig;
  private currentAbortController: AbortController | null = null;
  private currentRunId: number | null = null;

  constructor(config: SupervisorChatConfig = {}) {
    this.config = {
      maxRetries: config.maxRetries || 3,
      retryDelay: config.retryDelay || 1000,
    };
  }

  /**
   * Initialize the controller
   */
  async initialize(): Promise<void> {
    logger.info('[SupervisorChat] Initialized');
  }

  /**
   * Load conversation history from server
   * Returns messages in the format expected by the UI
   */
  async loadHistory(limit: number = 50): Promise<SupervisorChatMessage[]> {
    try {
      logger.info('[SupervisorChat] Loading history from server...');

      const url = toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/history?limit=${limit}`);
      const response = await fetch(url, {
        method: 'GET',
        credentials: 'include', // Cookie auth
      });

      if (!response.ok) {
        throw new Error(`Failed to load history: ${response.status} ${response.statusText}`);
      }

      const data = await response.json();

      // Transform server format to UI format
      // Server returns: { messages: Array<{ role, content, timestamp }>, total }
      const messages: SupervisorChatMessage[] = (data.messages || []).map((msg: any) => ({
        role: msg.role,
        content: msg.content,
        timestamp: new Date(msg.timestamp),
      }));

      logger.info(`[SupervisorChat] Loaded ${messages.length} messages from history`);
      return messages;
    } catch (error) {
      logger.error('[SupervisorChat] Failed to load history:', error);
      throw error;
    }
  }

  /**
   * Send a text message to the Supervisor and handle SSE response stream
   */
  async sendMessage(text: string): Promise<void> {
    if (!text || text.trim().length === 0) {
      throw new Error('Cannot send empty message');
    }

    const trimmedText = text.trim();
    logger.info('[SupervisorChat] Sending message:', trimmedText);

    // Cancel any previous stream
    if (this.currentAbortController) {
      this.currentAbortController.abort();
    }

    // Create new abort controller for this request
    this.currentAbortController = new AbortController();

    try {
      // Start SSE stream
      await this.streamChatResponse(trimmedText, this.currentAbortController.signal);

      logger.info('[SupervisorChat] Message sent and stream completed');
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        logger.info('[SupervisorChat] Message stream aborted');
        return;
      }

      logger.error('[SupervisorChat] Failed to send message:', error);
      throw error;
    } finally {
      this.currentAbortController = null;
      this.currentRunId = null;
    }
  }

  /**
   * Stream chat response via SSE
   */
  private async streamChatResponse(message: string, signal: AbortSignal): Promise<void> {
    const url = toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/chat`);

    logger.info('[SupervisorChat] Initiating SSE stream to:', url);

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
      },
      credentials: 'include', // Cookie auth
      body: JSON.stringify({ message }),
      signal,
    });

    logger.info(`[SupervisorChat] Response status: ${response.status} ${response.statusText}`);
    logger.info('[SupervisorChat] Response headers:', {
      contentType: response.headers.get('content-type'),
      transferEncoding: response.headers.get('transfer-encoding'),
      connection: response.headers.get('connection'),
    });

    if (!response.ok) {
      throw new Error(`Chat request failed: ${response.status} ${response.statusText}`);
    }

    if (!response.body) {
      throw new Error('No response body for SSE stream');
    }

    // Process SSE stream
    await this.processSSEStream(response.body, signal);
  }

  /**
   * Process Server-Sent Events stream from ReadableStream
   */
  private async processSSEStream(body: ReadableStream<Uint8Array>, signal: AbortSignal): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let chunkCount = 0;

    logger.info('[SupervisorChat] Starting to read SSE stream...');

    // Helper to process SSE messages from buffer
    const processBuffer = async (): Promise<string> => {
      // Normalize line endings and split on double newlines (SSE message boundary)
      const normalizedBuffer = buffer.replace(/\r\n/g, '\n');
      const parts = normalizedBuffer.split('\n\n');

      // Keep the last part as remaining buffer (might be incomplete)
      const remaining = parts.pop() || '';

      logger.debug(`[SupervisorChat] Processing ${parts.length} complete messages, remaining: ${remaining.length} chars`);

      for (const message of parts) {
        if (message.trim() === '') continue;

        const lines = message.split('\n');
        let eventType = '';
        let data = '';

        // Parse SSE message format
        for (const line of lines) {
          if (line.startsWith('event:')) {
            eventType = line.substring(6).trim();
          } else if (line.startsWith('data:')) {
            data = line.substring(5).trim();
          }
        }

        logger.info(`[SupervisorChat] Processing SSE event: ${eventType}`);

        // Handle the event
        if (data) {
          try {
            const parsedData = JSON.parse(data);
            await this.handleSSEEvent(eventType || 'message', parsedData);
          } catch (error) {
            logger.warn('[SupervisorChat] Failed to parse SSE data:', { data, error });
          }
        }
      }

      return remaining;
    };

    try {
      while (true) {
        // Check if aborted
        if (signal.aborted) {
          throw new Error('Stream aborted');
        }

        const { done, value } = await reader.read();
        chunkCount++;

        if (done) {
          logger.info(`[SupervisorChat] Stream ended after ${chunkCount} reads`);
          // Process any remaining complete messages in buffer
          if (buffer.trim()) {
            // Add a final \n\n to ensure the last message is processed
            buffer += '\n\n';
            await processBuffer();
          }
          break;
        }

        // Decode chunk and add to buffer
        const chunk = decoder.decode(value, { stream: true });
        buffer += chunk;
        logger.debug(`[SupervisorChat] Received chunk #${chunkCount} (${chunk.length} chars)`);

        // Process complete SSE messages
        buffer = await processBuffer();
      }
    } finally {
      reader.releaseLock();
    }
  }

  /**
   * Handle individual SSE events
   */
  private async handleSSEEvent(eventType: string, data: any): Promise<void> {
    logger.debug('[SupervisorChat] SSE event:', { eventType, data });

    // Handle connected event separately
    if (eventType === 'connected') {
      this.currentRunId = data.run_id;
      logger.info(`[SupervisorChat] Connected to run ${data.run_id}`);
      return;
    }

    // Handle heartbeat
    if (eventType === 'heartbeat') {
      logger.debug('[SupervisorChat] Heartbeat');
      return;
    }

    // All other events have format: { type: "...", payload: {...}, timestamp: "..." }
    const event = data as SSESupervisorEvent;
    const payload = event.payload || {};

    switch (eventType) {
      case 'supervisor_started':
        logger.info('[SupervisorChat] Supervisor started');
        if (payload.run_id) {
          this.currentRunId = payload.run_id;
        }
        // Emit supervisor started event for progress UI
        if (this.currentRunId) {
          eventBus.emit('supervisor:started', {
            runId: this.currentRunId,
            task: 'Processing message...',
            timestamp: Date.now(),
          });
        }
        break;

      case 'supervisor_thinking':
        logger.info('[SupervisorChat] Supervisor thinking:', payload.message);
        if (payload.message && this.currentRunId) {
          // Emit thinking event for progress UI
          eventBus.emit('supervisor:thinking', {
            message: payload.message,
            timestamp: Date.now(),
          });
        }
        break;

      case 'supervisor_complete':
        logger.info('[SupervisorChat] Supervisor complete');
        const result = payload.result;

        if (result && typeof result === 'string') {
          // Set streaming text incrementally (simulate streaming for smooth UX)
          conversationController.startStreaming();

          // Stream the result in chunks
          const chunkSize = 10; // characters per chunk
          for (let i = 0; i < result.length; i += chunkSize) {
            const chunk = result.substring(i, i + chunkSize);
            conversationController.appendStreaming(chunk);
            // Small delay for visual effect
            await new Promise(resolve => setTimeout(resolve, 10));
          }

          // Finalize the message
          await conversationController.finalizeStreaming();
        }

        // Clear supervisor progress UI
        if (this.currentRunId) {
          eventBus.emit('supervisor:complete', {
            runId: this.currentRunId,
            result: result || 'Task completed',
            status: 'success',
            timestamp: Date.now(),
          });
        }
        break;

      case 'error':
        logger.error('[SupervisorChat] Supervisor error:', payload.error || payload.message);
        const errorMsg = payload.error || payload.message || 'Unknown error';
        stateManager.showToast(`Error: ${errorMsg}`, 'error');

        if (this.currentRunId) {
          eventBus.emit('supervisor:error', {
            message: errorMsg,
            timestamp: Date.now(),
          });
        }
        break;

      default:
        logger.debug('[SupervisorChat] Unknown SSE event type:', { eventType, data });
    }
  }

  /**
   * Cancel current message stream
   */
  cancel(): void {
    if (this.currentAbortController) {
      logger.info('[SupervisorChat] Cancelling current stream');
      this.currentAbortController.abort();
      this.currentAbortController = null;
    }

    if (this.currentRunId) {
      eventBus.emit('supervisor:cleared', { timestamp: Date.now() });
      this.currentRunId = null;
    }
  }

  /**
   * Clear server-side conversation history
   * Creates a new Supervisor thread, effectively clearing all history
   */
  async clearHistory(): Promise<void> {
    try {
      logger.info('[SupervisorChat] Clearing server-side history...');

      const url = toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/history`);
      const response = await fetch(url, {
        method: 'DELETE',
        credentials: 'include', // Cookie auth
      });

      if (!response.ok) {
        throw new Error(`Failed to clear history: ${response.status} ${response.statusText}`);
      }

      logger.info('[SupervisorChat] Server-side history cleared');
    } catch (error) {
      logger.error('[SupervisorChat] Failed to clear history:', error);
      throw error;
    }
  }

  /**
   * Clean up resources
   */
  dispose(): void {
    this.cancel();
    logger.info('[SupervisorChat] Disposed');
  }
}
