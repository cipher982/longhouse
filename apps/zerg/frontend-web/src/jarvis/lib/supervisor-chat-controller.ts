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

import { logger } from '../core';
import { stateManager } from './state-manager';
import { conversationController } from './conversation-controller';
import { CONFIG, toAbsoluteUrl } from './config';
import { eventBus } from './event-bus';
import { workerProgressStore } from './worker-progress-store';
import {
  SSE_EVENT_TYPES,
  type SSEEventType,
  type SSEEventWrapper,
  type ConnectedPayload,
  type SupervisorStartedPayload,
  type SupervisorThinkingPayload,
  type SupervisorTokenPayload,
  type SupervisorCompletePayload,
  type SupervisorDeferredPayload,
  type SupervisorWaitingPayload,
  type SupervisorResumedPayload,
  type ErrorPayload,
  type WorkerSpawnedPayload,
  type WorkerStartedPayload,
  type WorkerCompletePayload,
  type WorkerSummaryReadyPayload,
  type WorkerToolStartedPayload,
  type WorkerToolCompletedPayload,
  type WorkerToolFailedPayload,
  type SupervisorToolStartedPayload,
  type SupervisorToolProgressPayload,
  type SupervisorToolCompletedPayload,
  type SupervisorToolFailedPayload,
} from '../../generated/sse-events';

export interface SupervisorChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;
  usage?: {
    prompt_tokens?: number | null;
    completion_tokens?: number | null;
    total_tokens?: number | null;
    reasoning_tokens?: number | null;
  };
}

export interface SupervisorChatConfig {
  maxRetries?: number;
  retryDelay?: number;
}

export class SupervisorChatController {
  private config: SupervisorChatConfig;
  private currentAbortController: AbortController | null = null;
  private currentRunId: number | null = null;
  private currentMessageId: string | null = null; // Backend-assigned message ID for the current run
  private lastCorrelationId: string | null = null;
  private lastMessageId: string | null = null; // Track messageId for cancellation (messageId-first pattern)
  private watchdogTimer: number | null = null;
  private isStreaming: boolean = false; // Track if we're receiving real tokens
  private isContinuationRun: boolean = false; // Track if current run is a continuation (prevents UI reset)
  private readonly WATCHDOG_TIMEOUT_MS = 60000;
  private onAnySseEventOnce: (() => void) | null = null;
  private lastEventId: number = 0; // Track last received event ID for resumption

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
    logger.debug('[SupervisorChat] Initialized');
  }

  /**
   * Load conversation history from server
   * Returns messages in the format expected by the UI
   */
  async loadHistory(limit: number = 50): Promise<SupervisorChatMessage[]> {
    try {
      logger.debug('[SupervisorChat] Loading history from server...');

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
      // Server returns: { messages: Array<{ role, content, timestamp, usage? }>, total }
      const messages: SupervisorChatMessage[] = (data.messages || []).map((msg: any) => ({
        role: msg.role,
        content: msg.content,
        timestamp: new Date(msg.timestamp),
        usage: msg.usage || undefined,
      }));

      logger.debug(`[SupervisorChat] Loaded ${messages.length} messages from history`);
      return messages;
    } catch (_error) {
      logger.error('[SupervisorChat] Failed to load history:', _error);
      throw _error;
    }
  }

  /**
   * Send a text message to the Supervisor and handle SSE response stream
   */
  async sendMessage(text: string, clientCorrelationId?: string, options?: { model?: string; reasoning_effort?: string }): Promise<void> {
    if (!text || text.trim().length === 0) {
      throw new Error('Cannot send empty message');
    }

    const trimmedText = text.trim();
    logger.debug(`[SupervisorChat] Sending message (clientCorrelationId=${clientCorrelationId}, model=${options?.model}, reasoning=${options?.reasoning_effort}): ${trimmedText}`);

    // Cancel any previous stream
    if (this.currentAbortController) {
      // Use messageId-first pattern for cancellation
      if (this.lastMessageId) {
        stateManager.updateAssistantStatusByMessageId(this.lastMessageId, 'canceled');
      } else if (this.lastCorrelationId) {
        // Fallback: Use correlationId if messageId not available (legacy behavior)
        stateManager.updateAssistantStatus(this.lastCorrelationId, 'canceled');
      }
      this.currentAbortController.abort();
    }

    this.lastCorrelationId = clientCorrelationId || null;
    this.lastMessageId = null; // Reset for new message (will be set on supervisor_started)

    // Create new abort controller for this request
    this.currentAbortController = new AbortController();

    // Start watchdog timer if we have a correlation ID
    if (clientCorrelationId) {
      this.startWatchdog(clientCorrelationId);
    }

    try {
      // Start SSE stream
      await this.streamChatResponse(trimmedText, this.currentAbortController.signal, clientCorrelationId, options);

      logger.debug('[SupervisorChat] Message sent and stream completed');
    } catch (_error) {
      if (_error instanceof Error && _error.name === 'AbortError') {
        logger.debug('[SupervisorChat] Message stream aborted');
        return;
      }

      logger.error('[SupervisorChat] Failed to send message:', _error);
      throw _error;
    } finally {
      this.clearWatchdog();
      this.currentAbortController = null;
      this.currentRunId = null;
      // Only clear if this was the correlation ID we were tracking for this call
      if (this.lastCorrelationId === clientCorrelationId) {
        this.lastCorrelationId = null;
        // Also clear lastMessageId since the request is complete
        this.lastMessageId = null;
      }
    }
  }

  /**
   * Start the 60s watchdog timer
   * v2.2: On timeout, show deferred message instead of error (work continues on server)
   */
  private startWatchdog(correlationId: string): void {
    this.clearWatchdog();
    this.watchdogTimer = window.setTimeout(async () => {
      logger.warn(`[SupervisorChat] Watchdog timeout for ${correlationId} - marking as deferred`);

      // v2.2: Don't cancel or show error - the server work continues in background
      const deferredMsg = 'Still working on this in the background. The server will continue processing...';

      // Show deferred message as assistant response (not error toast)
      conversationController.startStreaming(correlationId);
      conversationController.appendStreaming(deferredMsg, correlationId);
      await conversationController.finalizeStreaming();

      stateManager.updateAssistantStatus(correlationId, 'final', deferredMsg);

      // Emit deferred event for UI
      if (this.currentRunId) {
        eventBus.emit('supervisor:deferred', {
          runId: this.currentRunId,
          message: deferredMsg,
          timestamp: Date.now(),
        });
      }

      // DON'T call cancel() - let server continue working
    }, this.WATCHDOG_TIMEOUT_MS);
  }

  /**
   * Reset ("pet") the watchdog timer to keep the request alive
   */
  private petWatchdog(correlationId?: string): void {
    const id = correlationId ?? this.lastCorrelationId;
    if (this.watchdogTimer && id) {
      this.startWatchdog(id);
    }
  }

  /**
   * Clear any active watchdog timer
   */
  private clearWatchdog(): void {
    if (this.watchdogTimer !== null) {
      window.clearTimeout(this.watchdogTimer);
      this.watchdogTimer = null;
    }
  }

  /**
   * Stream chat response via SSE
   */
  private async streamChatResponse(
    message: string,
    signal: AbortSignal,
    client_correlation_id?: string,
    options?: { model?: string; reasoning_effort?: string }
  ): Promise<void> {
    const url = toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/chat`);

    logger.debug('[SupervisorChat] Initiating SSE stream to:', url);

    const requestBody: Record<string, unknown> = { message, client_correlation_id };
    if (options?.model) {
      requestBody.model = options.model;
    }
    if (options?.reasoning_effort) {
      requestBody.reasoning_effort = options.reasoning_effort;
    }

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
      },
      credentials: 'include', // Cookie auth
      body: JSON.stringify(requestBody),
      signal,
    });

    logger.debug(`[SupervisorChat] Response status: ${response.status} ${response.statusText}`);
    logger.debug('[SupervisorChat] Response headers:', {
      contentType: response.headers?.get?.('content-type') ?? null,
      transferEncoding: response.headers?.get?.('transfer-encoding') ?? null,
      connection: response.headers?.get?.('connection') ?? null,
    });

    if (!response.ok) {
      throw new Error(`Chat request failed: ${response.status} ${response.statusText}`);
    }

    const body = response.body;
    if (!body) {
      throw new Error('No response body for SSE stream');
    }

    // Process SSE stream
    await this.processSSEStream(body, signal);
  }

  /**
   * Process Server-Sent Events stream from ReadableStream
   */
  private async processSSEStream(body: ReadableStream<Uint8Array>, signal: AbortSignal): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let chunkCount = 0;

    logger.debug('[SupervisorChat] Starting to read SSE stream...');

    const abortDone = (): Promise<{ done: true; value: undefined }> =>
      new Promise((resolve) => {
        if (signal.aborted) {
          resolve({ done: true, value: undefined });
          return;
        }
        signal.addEventListener('abort', () => resolve({ done: true, value: undefined }), { once: true });
      });

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
        let eventId: number | null = null;

        // Parse SSE message format
        for (const line of lines) {
          if (line.startsWith('event:')) {
            eventType = line.substring(6).trim();
          } else if (line.startsWith('data:')) {
            data = line.substring(5).trim();
          } else if (line.startsWith('id:')) {
            // Extract event ID for resumption
            const idStr = line.substring(3).trim();
            const parsed = parseInt(idStr, 10);
            if (!isNaN(parsed)) {
              eventId = parsed;
            }
          }
        }

        // Update lastEventId if we got one
        if (eventId !== null) {
          this.lastEventId = eventId;
        }

        // Skip logging high-frequency token events to reduce console spam
        if (eventType && eventType !== 'supervisor_token') {
          logger.debug(`[SupervisorChat] Processing SSE event: ${eventType} (id=${eventId})`);
        }

        // Handle the event
        if (data) {
          try {
            const parsedData = JSON.parse(data);
            if (this.onAnySseEventOnce) {
              const fn = this.onAnySseEventOnce;
              this.onAnySseEventOnce = null;
              try {
                fn();
              } catch {
                // ignore
              }
            }
            // Validate eventType is a known SSE event type before handling
            // Uses generated SSE_EVENT_TYPES to prevent drift from schema
            if (eventType && (SSE_EVENT_TYPES as readonly string[]).includes(eventType)) {
              await this.handleSSEEvent(eventType as SSEEventType, parsedData);
            } else {
              logger.warn(`[SupervisorChat] Unknown SSE event type: ${eventType}`);
            }
          } catch (_error) {
            logger.warn('[SupervisorChat] Failed to parse SSE data:', { data, error: _error });
          }
        }
      }

      return remaining;
    };

    try {
      while (true) {
        const { done, value } = await Promise.race([reader.read(), abortDone()]);
        chunkCount++;

        if (done) {
          logger.debug(`[SupervisorChat] Stream ended after ${chunkCount} reads`);
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
   * Handle individual SSE events with type-safe payload access
   */
  private async handleSSEEvent(eventType: SSEEventType, data: unknown): Promise<void> {
    logger.debug('[SupervisorChat] SSE event:', { eventType, data });

    // Clear reconnecting state on first event (for resumable SSE reconnect flow)
    workerProgressStore.clearReconnecting();

    // Handle connected event separately (direct payload format)
    if (eventType === 'connected') {
      const payload = data as ConnectedPayload;
      this.petWatchdog(payload.client_correlation_id);
      this.currentRunId = payload.run_id;
      logger.debug(`[SupervisorChat] Connected to run ${payload.run_id}, correlationId: ${payload.client_correlation_id}`);
      if (payload.client_correlation_id) {
        stateManager.updateAssistantStatus(payload.client_correlation_id, 'typing');
      }
      return;
    }

    // Handle heartbeat (direct payload format)
    if (eventType === 'heartbeat') {
      this.petWatchdog();
      logger.debug('[SupervisorChat] Heartbeat');
      return;
    }

    // All other events have format: { type: "...", payload: {...}, client_correlation_id?: string }
    const wrapper = data as SSEEventWrapper<unknown>;
    const correlationId = wrapper.client_correlation_id;

    switch (eventType) {
      case 'supervisor_started': {
        const payload = wrapper.payload as SupervisorStartedPayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Supervisor started, message_id:', payload.message_id);
        if (payload.run_id) {
          this.currentRunId = payload.run_id;
        }

        // Store message_id for subsequent events (supervisor_token, supervisor_complete)
        this.currentMessageId = payload.message_id;
        this.lastMessageId = payload.message_id; // Track for cancellation (messageId-first pattern)

        // Check if this is a continuation run (worker result being processed)
        // continuation_of_message_id indicates the original message's ID
        const isContinuation = !!payload.continuation_of_message_id;
        this.isContinuationRun = isContinuation;

        if (isContinuation) {
          // Continuation: Create a NEW message bubble for the continuation response
          // This prevents overwriting the "delegating to worker" message
          logger.debug('[SupervisorChat] Continuation run detected, will create new message on first token');
          // Note: The new message will be created when the first token arrives.
          // We don't create it here to avoid an empty bubble appearing before content.
        } else if (correlationId && payload.message_id) {
          // Normal run: Bind messageId to the existing placeholder (found by correlationId)
          // After this, all updates should use messageId for lookup (messageId-first pattern)
          stateManager.bindMessageIdToCorrelationId(correlationId, payload.message_id, this.currentRunId ?? undefined);
          // Also set status to 'typing'
          stateManager.updateAssistantStatusByMessageId(payload.message_id, 'typing', undefined, undefined, this.currentRunId ?? undefined);
        } else if (correlationId) {
          // Fallback: no messageId available, use correlationId (legacy behavior)
          stateManager.updateAssistantStatus(correlationId, 'typing', undefined, undefined, this.currentRunId ?? undefined);
        }

        // Emit supervisor started event for progress UI
        if (this.currentRunId) {
          eventBus.emit('supervisor:started', {
            runId: this.currentRunId,
            task: payload.task || 'Processing message...',
            timestamp: Date.now(),
          });
        }
        break;
      }

      case 'supervisor_thinking': {
        const payload = wrapper.payload as SupervisorThinkingPayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Supervisor thinking:', payload.message);
        if (correlationId) {
          // If this is a continuation run, do NOT reset status to 'typing'
          // because 'typing' with no content wipes the existing message bubble.
          // The tokens will simply append when they arrive.
          if (!this.isContinuationRun) {
            stateManager.updateAssistantStatus(correlationId, 'typing');
          }
        }
        if (payload.message && this.currentRunId) {
          // Emit thinking event for progress UI
          eventBus.emit('supervisor:thinking', {
            message: payload.message,
            timestamp: Date.now(),
          });
        }
        break;
      }

      case 'supervisor_token': {
        const payload = wrapper.payload as SupervisorTokenPayload;
        // Real-time token streaming from LLM
        this.petWatchdog(correlationId);
        const token = payload.token;

        // Use message_id from the event if available, otherwise fall back to controller's stored value
        const messageId = payload.message_id || this.currentMessageId;

        if (token !== undefined && token !== null) {
          // Start streaming if not already
          if (!this.isStreaming) {
            this.isStreaming = true;

            if (this.isContinuationRun && messageId) {
              // Continuation: Create a NEW message with the message_id
              // This happens on first token of the continuation run
              logger.debug('[SupervisorChat] Starting streaming for continuation, messageId:', messageId);
              conversationController.startStreamingWithMessageId(messageId, this.currentRunId ?? undefined);
            } else if (messageId) {
              // Normal run with messageId: Use messageId-first pattern (messageId was bound on supervisor_started)
              logger.debug('[SupervisorChat] Starting streaming with messageId:', messageId);
              conversationController.startStreamingWithMessageId(messageId, this.currentRunId ?? undefined);
            } else {
              // Fallback: Use correlationId to find the placeholder (legacy behavior)
              conversationController.startStreaming(correlationId);
            }
          }

          // Append token to the streaming message using messageId-first pattern
          if (messageId) {
            conversationController.appendStreamingByMessageId(messageId, token);
          } else {
            // Fallback: Use correlationId (legacy behavior)
            conversationController.appendStreaming(token, correlationId);
          }
        }
        break;
      }

      case 'supervisor_complete': {
        const payload = wrapper.payload as SupervisorCompletePayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Supervisor complete');
        const result = payload.result;
        const wasStreaming = this.isStreaming;
        this.isStreaming = false; // Reset for next message

        // Use message_id from the event if available
        const messageId = payload.message_id || this.currentMessageId;

        if (result) {
          if (wasStreaming) {
            // Real tokens were streamed - just finalize the message
            logger.debug('[SupervisorChat] Finalizing real-time streamed response');
            await conversationController.finalizeStreaming();
          } else {
            // Fallback: No real tokens received (LLM_TOKEN_STREAM disabled?)
            // Stream the result in chunks for smooth UX
            logger.debug('[SupervisorChat] Fallback: simulating streaming for response');

            // Use messageId-first pattern for ALL runs
            if (messageId) {
              conversationController.startStreamingWithMessageId(messageId, this.currentRunId ?? undefined);
            } else {
              // Fallback: Use correlationId (legacy behavior)
              conversationController.startStreaming(correlationId);
            }

            const chunkSize = 10; // characters per chunk
            for (let i = 0; i < result.length; i += chunkSize) {
              const chunk = result.substring(i, i + chunkSize);
              if (messageId) {
                conversationController.appendStreamingByMessageId(messageId, chunk);
              } else {
                conversationController.appendStreaming(chunk, correlationId);
              }
              // Small delay for visual effect
              await new Promise(resolve => setTimeout(resolve, 10));
            }

            await conversationController.finalizeStreaming();
          }

          // Update the message status to final using messageId-first pattern
          if (messageId) {
            stateManager.updateAssistantStatusByMessageId(messageId, 'final', result, payload.usage, this.currentRunId ?? undefined);
          } else if (correlationId) {
            // Fallback: Use correlationId (legacy behavior)
            stateManager.updateAssistantStatus(correlationId, 'final', result, payload.usage, this.currentRunId ?? undefined);
          }
        }

        // Clear supervisor progress UI
        if (this.currentRunId) {
          eventBus.emit('supervisor:complete', {
            runId: this.currentRunId,
            result: result || 'Task completed',
            status: 'success',
            timestamp: Date.now(),
            // Token usage for debug/power mode
            usage: payload.usage,
          });
        }

        // Reset continuation flag for next run
        this.isContinuationRun = false;
        this.currentMessageId = null;
        break;
      }

      case 'supervisor_deferred': {
        const payload = wrapper.payload as SupervisorDeferredPayload;
        // Timeout migration: run continues in background, we show a friendly message
        this.clearWatchdog();
        logger.debug('[SupervisorChat] Supervisor deferred (timeout migration)');
        const deferredMsg = payload.message || 'Still working on this in the background...';

        // Show deferred message as an assistant response (not error toast)
        conversationController.startStreaming(correlationId);
        conversationController.appendStreaming(deferredMsg, correlationId);
        await conversationController.finalizeStreaming();

        if (correlationId) {
          // Use 'final' status since this is a valid response
          stateManager.updateAssistantStatus(correlationId, 'final', deferredMsg);
        }

        if (this.currentRunId) {
          eventBus.emit('supervisor:deferred', {
            runId: this.currentRunId,
            message: deferredMsg,
            attachUrl: payload.attach_url,
            timestamp: Date.now(),
          });
        }
        break;
      }

      case 'supervisor_waiting': {
        const payload = wrapper.payload as SupervisorWaitingPayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Supervisor waiting (interrupt):', payload.job_id);

        const waitingMsg = payload.message || 'Working on this in the background...';
        const messageId = payload.message_id || this.currentMessageId;

        // Update the existing assistant bubble with a stable "waiting" message.
        // The final response will arrive later as supervisor_tokens/supervisor_complete for the same message_id.
        if (messageId) {
          stateManager.updateAssistantStatusByMessageId(messageId, 'final', waitingMsg, undefined, this.currentRunId ?? undefined);
        } else if (correlationId) {
          stateManager.updateAssistantStatus(correlationId, 'final', waitingMsg);
        }

        if (this.currentRunId) {
          eventBus.emit('supervisor:waiting', {
            runId: this.currentRunId,
            jobId: payload.job_id,
            message: waitingMsg,
            timestamp: Date.now(),
          });
        }
        break;
      }

      case 'supervisor_resumed': {
        const payload = wrapper.payload as SupervisorResumedPayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Supervisor resumed (interrupt)');

        const messageId = payload.message_id || this.currentMessageId;
        if (messageId) {
          // Set status back to typing; tokens will overwrite the waiting message as they stream in.
          stateManager.updateAssistantStatusByMessageId(messageId, 'typing', undefined, undefined, this.currentRunId ?? undefined);
        } else if (correlationId) {
          stateManager.updateAssistantStatus(correlationId, 'typing');
        }

        if (this.currentRunId) {
          eventBus.emit('supervisor:resumed', {
            runId: this.currentRunId,
            timestamp: Date.now(),
          });
        }
        break;
      }

      case 'error': {
        const payload = wrapper.payload as ErrorPayload;
        logger.error('[SupervisorChat] Supervisor error:', payload.error || payload.message);
        const errorMsg = payload.error || payload.message || 'Unknown error';
        stateManager.showToast(`Error: ${errorMsg}`, 'error');

        if (correlationId) {
          stateManager.updateAssistantStatus(correlationId, 'error');
        }

        if (this.currentRunId) {
          eventBus.emit('supervisor:error', {
            message: errorMsg,
            timestamp: Date.now(),
          });
        }
        break;
      }

      // ===== Worker lifecycle events (v2.1) =====
      case 'worker_spawned': {
        const payload = wrapper.payload as WorkerSpawnedPayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Worker spawned:', payload.job_id);
        eventBus.emit('supervisor:worker_spawned', {
          jobId: payload.job_id,
          task: payload.task,
          timestamp: Date.now(),
        });
        break;
      }

      case 'worker_started': {
        const payload = wrapper.payload as WorkerStartedPayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Worker started:', payload.job_id);
        eventBus.emit('supervisor:worker_started', {
          jobId: payload.job_id,
          workerId: payload.worker_id,
          timestamp: Date.now(),
        });
        break;
      }

      case 'worker_complete': {
        const payload = wrapper.payload as WorkerCompletePayload;
        this.petWatchdog(correlationId);
        logger.debug(`[SupervisorChat] Worker complete: job=${payload.job_id} status=${payload.status}`);
        eventBus.emit('supervisor:worker_complete', {
          jobId: payload.job_id,
          workerId: payload.worker_id,
          status: payload.status,
          durationMs: payload.duration_ms,
          timestamp: Date.now(),
        });
        break;
      }

      case 'worker_summary_ready': {
        const payload = wrapper.payload as WorkerSummaryReadyPayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Worker summary ready:', payload.job_id);
        eventBus.emit('supervisor:worker_summary', {
          jobId: payload.job_id,
          workerId: payload.worker_id,
          summary: payload.summary,
          timestamp: Date.now(),
        });
        break;
      }

      // ===== Worker tool events (v2.1 Activity Ticker) =====
      case 'worker_tool_started': {
        const payload = wrapper.payload as WorkerToolStartedPayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Worker tool started:', payload.tool_name);
        eventBus.emit('worker:tool_started', {
          workerId: payload.worker_id,
          toolName: payload.tool_name,
          toolCallId: payload.tool_call_id,
          argsPreview: payload.tool_args_preview,
          timestamp: Date.now(),
        });
        break;
      }

      case 'worker_tool_completed': {
        const payload = wrapper.payload as WorkerToolCompletedPayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Worker tool completed:', payload.tool_name);
        eventBus.emit('worker:tool_completed', {
          workerId: payload.worker_id,
          toolName: payload.tool_name,
          toolCallId: payload.tool_call_id,
          durationMs: payload.duration_ms,
          resultPreview: payload.result_preview,
          timestamp: Date.now(),
        });
        break;
      }

      case 'worker_tool_failed': {
        const payload = wrapper.payload as WorkerToolFailedPayload;
        this.petWatchdog(correlationId);
        logger.warn(`[SupervisorChat] Worker tool failed: ${payload.tool_name} - ${payload.error}`);
        eventBus.emit('worker:tool_failed', {
          workerId: payload.worker_id,
          toolName: payload.tool_name,
          toolCallId: payload.tool_call_id,
          durationMs: payload.duration_ms,
          error: payload.error,
          timestamp: Date.now(),
        });
        break;
      }

      // ===== Supervisor tool events (uniform treatment with worker tools) =====
      case 'supervisor_tool_started': {
        const payload = wrapper.payload as SupervisorToolStartedPayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Supervisor tool started:', payload.tool_name);
        eventBus.emit('supervisor:tool_started', {
          runId: payload.run_id ?? this.currentRunId ?? 0,
          toolName: payload.tool_name,
          toolCallId: payload.tool_call_id,
          argsPreview: payload.tool_args_preview,
          args: payload.tool_args,
          timestamp: Date.now(),
        });
        break;
      }

      case 'supervisor_tool_progress': {
        const payload = wrapper.payload as SupervisorToolProgressPayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Supervisor tool progress:', payload.message);
        eventBus.emit('supervisor:tool_progress', {
          runId: payload.run_id ?? this.currentRunId ?? 0,
          toolCallId: payload.tool_call_id,
          message: payload.message,
          level: payload.level,
          progressPct: payload.progress_pct,
          data: payload.data,
          timestamp: Date.now(),
        });
        break;
      }

      case 'supervisor_tool_completed': {
        const payload = wrapper.payload as SupervisorToolCompletedPayload;
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Supervisor tool completed:', payload.tool_name);
        eventBus.emit('supervisor:tool_completed', {
          runId: payload.run_id ?? this.currentRunId ?? 0,
          toolName: payload.tool_name,
          toolCallId: payload.tool_call_id,
          durationMs: payload.duration_ms,
          resultPreview: payload.result_preview,
          result: payload.result,
          timestamp: Date.now(),
        });
        break;
      }

      case 'supervisor_tool_failed': {
        const payload = wrapper.payload as SupervisorToolFailedPayload;
        this.petWatchdog(correlationId);
        logger.warn(`[SupervisorChat] Supervisor tool failed: ${payload.tool_name} - ${payload.error}`);
        eventBus.emit('supervisor:tool_failed', {
          runId: payload.run_id ?? this.currentRunId ?? 0,
          toolName: payload.tool_name,
          toolCallId: payload.tool_call_id,
          durationMs: payload.duration_ms,
          error: payload.error,
          errorDetails: payload.error_details,
          timestamp: Date.now(),
        });
        break;
      }

      default:
        logger.debug('[SupervisorChat] Unknown SSE event type:', { eventType, data });
    }
  }

  /**
   * Cancel current message stream
   */
  cancel(): void {
    if (this.currentAbortController) {
      logger.debug('[SupervisorChat] Cancelling current stream');
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
      logger.debug('[SupervisorChat] Clearing server-side history...');

      const url = toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/history`);
      const response = await fetch(url, {
        method: 'DELETE',
        credentials: 'include', // Cookie auth
      });

      if (!response.ok) {
        throw new Error(`Failed to clear history: ${response.status} ${response.statusText}`);
      }

      logger.debug('[SupervisorChat] Server-side history cleared');
    } catch (_error) {
      logger.error('[SupervisorChat] Failed to clear history:', _error);
      throw _error;
    }
  }

  /**
   * Attach to an existing run's event stream
   * Used for reconnecting after page refresh (Resumable SSE v1)
   */
  async attachToRun(runId: number): Promise<void> {
    logger.debug(`[SupervisorChat] Attaching to run ${runId} (lastEventId=${this.lastEventId})...`);

    // Cancel any current stream
    if (this.currentAbortController) {
      this.currentAbortController.abort();
    }

    // Create new abort controller
    this.currentAbortController = new AbortController();
    this.currentRunId = runId;

    try {
      // Use new resumable SSE endpoint: /api/stream/runs/{run_id}
      // This endpoint supports replay from lastEventId
      const url = new URL(toAbsoluteUrl(`/api/stream/runs/${runId}`));

      // Add resumption parameter if we have a last event ID
      if (this.lastEventId > 0) {
        url.searchParams.set('after_event_id', String(this.lastEventId));
        logger.debug(`[SupervisorChat] Resuming from event ID: ${this.lastEventId}`);
      }

      const headers: Record<string, string> = {
        'Accept': 'text/event-stream',
      };

      // Alternative: Use Last-Event-ID header (SSE standard)
      // The backend prefers this over query param
      if (this.lastEventId > 0) {
        headers['Last-Event-ID'] = String(this.lastEventId);
      }

      const response = await fetch(url.toString(), {
        method: 'GET',
        headers,
        credentials: 'include',
        signal: this.currentAbortController.signal,
      });

      if (!response.ok) {
        throw new Error(`Failed to attach to run: ${response.status} ${response.statusText}`);
      }

      const body = response.body;
      if (!body) {
        throw new Error('No response body for SSE stream');
      }

      // Start SSE processing in the background.
      //
      // Important: attachToRun() must resolve quickly (on first SSE event) so the UI
      // can stop showing "reconnecting..." while the run is still active.
      const signal = this.currentAbortController.signal;
      const ready = new Promise<void>((resolve) => {
        this.onAnySseEventOnce = resolve;
      });

      void this.processSSEStream(body, signal)
        .then(() => {
          logger.debug(`[SupervisorChat] Attached to run ${runId} and stream completed`);
        })
        .catch((error) => {
          if (error instanceof Error && error.name === 'AbortError') {
            logger.debug(`[SupervisorChat] Attach to run ${runId} aborted`);
            return;
          }
          logger.error(`[SupervisorChat] SSE stream error while attached to run ${runId}:`, error);
        })
        .finally(() => {
          if (this.currentRunId === runId) {
            this.currentAbortController = null;
            this.currentRunId = null;
            this.onAnySseEventOnce = null;
          }
        });

      // Wait until the stream is actually live (first event/heartbeat), but don't block forever.
      await Promise.race([ready, new Promise<void>((resolve) => setTimeout(resolve, 1500))]);
    } catch (_error) {
      if (_error instanceof Error && _error.name === 'AbortError') {
        logger.debug(`[SupervisorChat] Attach to run ${runId} aborted`);
        return;
      }

      logger.error(`[SupervisorChat] Failed to attach to run ${runId}:`, _error);
      throw _error;
    }
  }

  /**
   * Clean up resources
   */
  dispose(): void {
    this.cancel();
    logger.debug('[SupervisorChat] Disposed');
  }
}
