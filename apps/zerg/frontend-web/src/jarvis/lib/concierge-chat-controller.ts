/**
 * ConciergeChatController - Manages text chat with Concierge via Zerg backend
 *
 * Responsibilities:
 * - Send messages to POST /api/jarvis/chat (SSE streaming)
 * - Handle SSE stream for streaming responses
 * - Load conversation history from GET /api/jarvis/history
 * - Emit events for UI updates via stateManager
 *
 * Usage:
 *   const controller = new ConciergeChatController();
 *   await controller.initialize();
 *   await controller.sendMessage("Hello, assistant!");
 */

import { logger } from '../core';
import { stateManager } from './state-manager';
import { conversationController } from './conversation-controller';
import { CONFIG, toAbsoluteUrl } from './config';
import { eventBus } from './event-bus';
import { commisProgressStore } from './commis-progress-store';
import {
  SSE_EVENT_TYPES,
  type SSEEventType,
  type SSEEventWrapper,
  type ConnectedPayload,
  type ConciergeStartedPayload,
  type ConciergeThinkingPayload,
  type ConciergeTokenPayload,
  type ConciergeCompletePayload,
  type ConciergeDeferredPayload,
  type ConciergeWaitingPayload,
  type ConciergeResumedPayload,
  type ErrorPayload,
  type CommisSpawnedPayload,
  type CommisStartedPayload,
  type CommisCompletePayload,
  type CommisSummaryReadyPayload,
  type CommisToolStartedPayload,
  type CommisToolCompletedPayload,
  type CommisToolFailedPayload,
  type ConciergeToolStartedPayload,
  type ConciergeToolProgressPayload,
  type ConciergeToolCompletedPayload,
  type ConciergeToolFailedPayload,
} from '../../generated/sse-events';

export interface CommisToolInfo {
  tool_name: string;
  status: string;
  duration_ms?: number;
  result_preview?: string;
  error?: string;
}

export interface CommisInfo {
  job_id: number;
  task: string;
  status: string;
  summary?: string;
  tools: CommisToolInfo[];
}

export interface ToolCallInfo {
  tool_call_id: string;
  tool_name: string;
  args?: Record<string, unknown>;
  result?: string;
  commis?: CommisInfo;
}

export interface ConciergeChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;
  usage?: {
    prompt_tokens?: number | null;
    completion_tokens?: number | null;
    total_tokens?: number | null;
    reasoning_tokens?: number | null;
  };
  tool_calls?: ToolCallInfo[];
}

export interface ConciergeChatConfig {
  maxRetries?: number;
  retryDelay?: number;
}

export class ConciergeChatController {
  private config: ConciergeChatConfig;
  private currentAbortController: AbortController | null = null;
  private currentCourseId: number | null = null;
  private currentMessageId: string | null = null; // Client-generated message ID for the current course
  private lastMessageId: string | null = null; // Track messageId for cancellation
  private watchdogTimer: number | null = null;
  private isStreaming: boolean = false; // Track if we're receiving real tokens
  private isContinuationCourse: boolean = false; // Track if current course is a continuation (prevents UI reset)
  private readonly WATCHDOG_TIMEOUT_MS = 60000;
  private onAnySseEventOnce: (() => void) | null = null;
  private lastEventId: number = 0; // Track last received event ID for resumption

  constructor(config: ConciergeChatConfig = {}) {
    this.config = {
      maxRetries: config.maxRetries || 3,
      retryDelay: config.retryDelay || 1000,
    };
  }

  /**
   * Initialize the controller
   */
  async initialize(): Promise<void> {
    logger.debug('[ConciergeChat] Initialized');
  }

  /**
   * Load conversation history from server
   * Returns messages in the format expected by the UI
   */
  async loadHistory(limit: number = 50): Promise<ConciergeChatMessage[]> {
    try {
      logger.debug('[ConciergeChat] Loading history from server...');

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
      // Server returns: { messages: Array<{ role, content, timestamp, usage?, tool_calls? }>, total }
      const messages: ConciergeChatMessage[] = (data.messages || []).map((msg: any) => ({
        role: msg.role,
        content: msg.content,
        timestamp: new Date(msg.timestamp),
        usage: msg.usage || undefined,
        tool_calls: msg.tool_calls || undefined,
      }));

      logger.debug(`[ConciergeChat] Loaded ${messages.length} messages from history`);
      return messages;
    } catch (_error) {
      logger.error('[ConciergeChat] Failed to load history:', _error);
      throw _error;
    }
  }

  /**
   * Send a text message to the Concierge and handle SSE response stream
   */
  async sendMessage(text: string, messageId: string, options?: { model?: string; reasoning_effort?: string; replay_scenario?: string }): Promise<void> {
    if (!text || text.trim().length === 0) {
      throw new Error('Cannot send empty message');
    }

    const trimmedText = text.trim();
    logger.debug(`[ConciergeChat] Sending message (messageId=${messageId}, model=${options?.model}, reasoning=${options?.reasoning_effort}): ${trimmedText}`);

    // Cancel any previous stream
    if (this.currentAbortController) {
      if (this.lastMessageId) {
        stateManager.updateAssistantStatusByMessageId(this.lastMessageId, 'canceled');
      }
      this.currentAbortController.abort();
    }

    // Store messageId for this message (client-generated, no binding needed)
    this.currentMessageId = messageId;
    this.lastMessageId = messageId;

    // Create new abort controller for this request
    this.currentAbortController = new AbortController();

    // Start watchdog timer
    this.startWatchdog(messageId);

    try {
      // Start SSE stream
      await this.streamChatResponse(trimmedText, this.currentAbortController.signal, messageId, options);

      logger.debug('[ConciergeChat] Message sent and stream completed');
    } catch (_error) {
      if (_error instanceof Error && _error.name === 'AbortError') {
        logger.debug('[ConciergeChat] Message stream aborted');
        return;
      }

      logger.error('[ConciergeChat] Failed to send message:', _error);
      throw _error;
    } finally {
      this.clearWatchdog();
      this.currentAbortController = null;
      this.currentCourseId = null;
      // Clear messageId tracking since the request is complete
      if (this.lastMessageId === messageId) {
        this.lastMessageId = null;
      }
    }
  }

  /**
   * Start the 60s watchdog timer
   * v2.2: On timeout, show deferred message instead of error (work continues on server)
   */
  private startWatchdog(messageId: string): void {
    this.clearWatchdog();
    this.watchdogTimer = window.setTimeout(async () => {
      logger.warn(`[ConciergeChat] Watchdog timeout for ${messageId} - marking as deferred`);

      // v2.2: Don't cancel or show error - the server work continues in background
      const deferredMsg = 'Still working on this in the background. The server will continue processing...';

      // Show deferred message as assistant response (not error toast)
      conversationController.startStreamingWithMessageId(messageId, this.currentCourseId ?? undefined);
      conversationController.appendStreamingByMessageId(messageId, deferredMsg);
      await conversationController.finalizeStreaming();

      stateManager.updateAssistantStatusByMessageId(messageId, 'final', deferredMsg);

      // Emit deferred event for UI
      if (this.currentCourseId) {
        eventBus.emit('concierge:deferred', {
          courseId: this.currentCourseId,
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
  private petWatchdog(): void {
    if (this.watchdogTimer && this.currentMessageId) {
      this.startWatchdog(this.currentMessageId);
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
    messageId: string,
    options?: { model?: string; reasoning_effort?: string; replay_scenario?: string }
  ): Promise<void> {
    const url = toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/chat`);

    logger.debug('[ConciergeChat] Initiating SSE stream to:', url);

    const requestBody: Record<string, unknown> = { message, message_id: messageId };
    if (options?.model) {
      requestBody.model = options.model;
    }
    if (options?.reasoning_effort) {
      requestBody.reasoning_effort = options.reasoning_effort;
    }
    // Video recording: pass replay scenario from window global or options
    const replayScenario = options?.replay_scenario || (window as Window & { __REPLAY_SCENARIO?: string }).__REPLAY_SCENARIO;
    if (replayScenario) {
      requestBody.replay_scenario = replayScenario;
      logger.debug('[ConciergeChat] Using replay scenario:', replayScenario);
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

    logger.debug(`[ConciergeChat] Response status: ${response.status} ${response.statusText}`);
    logger.debug('[ConciergeChat] Response headers:', {
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

    logger.debug('[ConciergeChat] Starting to read SSE stream...');

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

      logger.debug(`[ConciergeChat] Processing ${parts.length} complete messages, remaining: ${remaining.length} chars`);

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
        if (eventType && eventType !== 'concierge_token') {
          logger.debug(`[ConciergeChat] Processing SSE event: ${eventType} (id=${eventId})`);
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
              logger.warn(`[ConciergeChat] Unknown SSE event type: ${eventType}`);
            }
          } catch (_error) {
            logger.warn('[ConciergeChat] Failed to parse SSE data:', { data, error: _error });
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
          logger.debug(`[ConciergeChat] Stream ended after ${chunkCount} reads`);
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
        logger.debug(`[ConciergeChat] Received chunk #${chunkCount} (${chunk.length} chars)`);

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
    logger.debug('[ConciergeChat] SSE event:', { eventType, data });

    // Clear reconnecting state on first event (for resumable SSE reconnect flow)
    commisProgressStore.clearReconnecting();

    // Handle connected event separately (direct payload format)
    if (eventType === 'connected') {
      const payload = data as ConnectedPayload;
      this.petWatchdog();
      this.currentCourseId = payload.course_id;
      logger.debug(`[ConciergeChat] Connected to course ${payload.course_id}`);
      // Update placeholder status to typing using client-generated messageId
      if (this.currentMessageId) {
        stateManager.updateAssistantStatusByMessageId(this.currentMessageId, 'typing');
      }
      return;
    }

    // Handle heartbeat (direct payload format)
    if (eventType === 'heartbeat') {
      this.petWatchdog();
      logger.debug('[ConciergeChat] Heartbeat');
      return;
    }

    // All other events have format: { type: "...", payload: {...} }
    const wrapper = data as SSEEventWrapper<unknown>;

    switch (eventType) {
      case 'concierge_started': {
        const payload = wrapper.payload as ConciergeStartedPayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Concierge started, message_id:', payload.message_id);
        if (payload.course_id) {
          this.currentCourseId = payload.course_id;
        }

        // Check if this is a continuation course (commis result being processed)
        // continuation_of_message_id indicates the original message's ID
        const isContinuation = !!payload.continuation_of_message_id;
        this.isContinuationCourse = isContinuation;

        if (isContinuation) {
          // Continuation: Backend generates NEW messageId, store it for streaming
          // The new message bubble will be created when the first token arrives
          this.currentMessageId = payload.message_id;
          this.lastMessageId = payload.message_id;
          logger.debug('[ConciergeChat] Continuation course detected, will create new message on first token');
        } else {
          // Normal course: Client already generated messageId, just confirm it matches
          // The placeholder was already created with this messageId, so updates will work
          logger.debug('[ConciergeChat] Normal course, using client messageId:', this.currentMessageId);
          // Set status to 'typing'
          if (this.currentMessageId) {
            stateManager.updateAssistantStatusByMessageId(this.currentMessageId, 'typing', undefined, undefined, this.currentCourseId ?? undefined);
          }
        }

        // Emit concierge started event for progress UI (include trace_id for debugging)
        if (this.currentCourseId) {
          eventBus.emit('concierge:started', {
            courseId: this.currentCourseId,
            task: payload.task || 'Processing message...',
            timestamp: Date.now(),
            traceId: payload.trace_id,
          });
        }
        break;
      }

      case 'concierge_thinking': {
        const payload = wrapper.payload as ConciergeThinkingPayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Concierge thinking:', payload.message);
        // If this is a continuation course, do NOT reset status to 'typing'
        // because 'typing' with no content wipes the existing message bubble.
        if (!this.isContinuationCourse && this.currentMessageId) {
          stateManager.updateAssistantStatusByMessageId(this.currentMessageId, 'typing');
        }
        if (payload.message && this.currentCourseId) {
          // Emit thinking event for progress UI
          eventBus.emit('concierge:thinking', {
            message: payload.message,
            timestamp: Date.now(),
          });
        }
        break;
      }

      case 'concierge_token': {
        const payload = wrapper.payload as ConciergeTokenPayload;
        // Real-time token streaming from LLM
        this.petWatchdog();
        const token = payload.token;

        // Use message_id from the event if available, otherwise fall back to controller's stored value
        const messageId = payload.message_id || this.currentMessageId;

        if (token !== undefined && token !== null && messageId) {
          // Start streaming if not already
          if (!this.isStreaming) {
            this.isStreaming = true;
            logger.debug('[ConciergeChat] Starting streaming with messageId:', messageId);
            conversationController.startStreamingWithMessageId(messageId, this.currentCourseId ?? undefined);
          }

          // Append token to the streaming message
          conversationController.appendStreamingByMessageId(messageId, token);
        }
        break;
      }

      case 'concierge_complete': {
        const payload = wrapper.payload as ConciergeCompletePayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Concierge complete');
        const result = payload.result;
        const wasStreaming = this.isStreaming;
        this.isStreaming = false; // Reset for next message

        // Use message_id from the event if available
        const messageId = payload.message_id || this.currentMessageId;

        if (result && messageId) {
          if (wasStreaming) {
            // Real tokens were streamed - just finalize the message
            logger.debug('[ConciergeChat] Finalizing real-time streamed response');
            await conversationController.finalizeStreaming();
          } else {
            // Fallback: No real tokens received (LLM_TOKEN_STREAM disabled?)
            // Stream the result in chunks for smooth UX
            logger.debug('[ConciergeChat] Fallback: simulating streaming for response');

            conversationController.startStreamingWithMessageId(messageId, this.currentCourseId ?? undefined);

            const chunkSize = 10; // characters per chunk
            for (let i = 0; i < result.length; i += chunkSize) {
              const chunk = result.substring(i, i + chunkSize);
              conversationController.appendStreamingByMessageId(messageId, chunk);
              // Small delay for visual effect
              await new Promise(resolve => setTimeout(resolve, 10));
            }

            await conversationController.finalizeStreaming();
          }

          // Update the message status to final
          stateManager.updateAssistantStatusByMessageId(messageId, 'final', result, payload.usage, this.currentCourseId ?? undefined);
        }

        // Clear concierge progress UI (include trace_id for debugging)
        if (this.currentCourseId) {
          eventBus.emit('concierge:complete', {
            courseId: this.currentCourseId,
            result: result || 'Task completed',
            status: 'success',
            timestamp: Date.now(),
            // Token usage for debug/power mode
            usage: payload.usage,
            traceId: payload.trace_id,
          });
        }

        // Reset continuation flag for next course
        this.isContinuationCourse = false;
        this.currentMessageId = null;
        this.lastMessageId = null;
        break;
      }

      case 'concierge_deferred': {
        const payload = wrapper.payload as ConciergeDeferredPayload;
        // Timeout migration: course continues in background, we show a friendly message
        this.clearWatchdog();
        logger.debug('[ConciergeChat] Concierge deferred (timeout migration)');
        const deferredMsg = payload.message || 'Still working on this in the background...';
        const messageId = payload.message_id || this.currentMessageId;

        // Show deferred message as an assistant response (not error toast)
        if (messageId) {
          conversationController.startStreamingWithMessageId(messageId, this.currentCourseId ?? undefined);
          conversationController.appendStreamingByMessageId(messageId, deferredMsg);
          await conversationController.finalizeStreaming();
          // Use 'final' status since this is a valid response
          stateManager.updateAssistantStatusByMessageId(messageId, 'final', deferredMsg);
        }

        if (this.currentCourseId) {
          eventBus.emit('concierge:deferred', {
            courseId: this.currentCourseId,
            message: deferredMsg,
            attachUrl: payload.attach_url,
            timestamp: Date.now(),
          });
        }
        this.lastMessageId = null;
        break;
      }

      case 'concierge_waiting': {
        const payload = wrapper.payload as ConciergeWaitingPayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Concierge waiting (interrupt):', payload.job_id);

        const messageId = payload.message_id || this.currentMessageId;

        // Keep assistant bubble in 'typing' state while waiting for commis.
        // Don't show the interrupt message - the commis card displays task details.
        // Final response arrives later via concierge_tokens/concierge_complete.
        if (messageId) {
          stateManager.updateAssistantStatusByMessageId(messageId, 'typing', undefined, undefined, this.currentCourseId ?? undefined);
        }

        if (this.currentCourseId) {
          eventBus.emit('concierge:waiting', {
            courseId: this.currentCourseId,
            jobId: payload.job_id,
            message: payload.message,  // Keep for event bus consumers (progress UI)
            timestamp: Date.now(),
          });
        }
        break;
      }

      case 'concierge_resumed': {
        const payload = wrapper.payload as ConciergeResumedPayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Concierge resumed (interrupt)');

        const messageId = payload.message_id || this.currentMessageId;
        if (messageId) {
          // Set status back to typing; tokens will overwrite the waiting message as they stream in.
          stateManager.updateAssistantStatusByMessageId(messageId, 'typing', undefined, undefined, this.currentCourseId ?? undefined);
        }

        if (this.currentCourseId) {
          eventBus.emit('concierge:resumed', {
            courseId: this.currentCourseId,
            timestamp: Date.now(),
          });
        }
        break;
      }

      case 'error': {
        const payload = wrapper.payload as ErrorPayload;
        logger.error('[ConciergeChat] Concierge error:', payload.error || payload.message);
        const errorMsg = payload.error || payload.message || 'Unknown error';
        stateManager.showToast(`Error: ${errorMsg}`, 'error');

        if (this.currentMessageId) {
          stateManager.updateAssistantStatusByMessageId(this.currentMessageId, 'error');
        }

        if (this.currentCourseId) {
          eventBus.emit('concierge:error', {
            message: errorMsg,
            timestamp: Date.now(),
            traceId: payload.trace_id,
            courseId: payload.course_id ?? this.currentCourseId,
          });
        }

        this.lastMessageId = null;
        break;
      }

      // ===== Commis lifecycle events (v2.1) =====
      case 'commis_spawned': {
        const payload = wrapper.payload as CommisSpawnedPayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Commis spawned:', payload.job_id);
        eventBus.emit('concierge:commis_spawned', {
          jobId: payload.job_id,
          toolCallId: payload.tool_call_id,
          task: payload.task,
          timestamp: Date.now(),
          courseId: payload.course_id ?? this.currentCourseId ?? undefined,
        });
        break;
      }

      case 'commis_started': {
        const payload = wrapper.payload as CommisStartedPayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Commis started:', payload.job_id);
        eventBus.emit('concierge:commis_started', {
          jobId: payload.job_id,
          commisId: payload.commis_id,
          timestamp: Date.now(),
          courseId: payload.course_id ?? this.currentCourseId ?? undefined,
        });
        break;
      }

      case 'commis_complete': {
        const payload = wrapper.payload as CommisCompletePayload;
        this.petWatchdog();
        logger.debug(`[ConciergeChat] Commis complete: job=${payload.job_id} status=${payload.status}`);
        eventBus.emit('concierge:commis_complete', {
          jobId: payload.job_id,
          commisId: payload.commis_id,
          status: payload.status,
          durationMs: payload.duration_ms,
          timestamp: Date.now(),
          courseId: payload.course_id ?? this.currentCourseId ?? undefined,
        });
        break;
      }

      case 'commis_summary_ready': {
        const payload = wrapper.payload as CommisSummaryReadyPayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Commis summary ready:', payload.job_id);
        eventBus.emit('concierge:commis_summary', {
          jobId: payload.job_id,
          commisId: payload.commis_id,
          summary: payload.summary,
          timestamp: Date.now(),
          courseId: payload.course_id ?? this.currentCourseId ?? undefined,
        });
        break;
      }

      // ===== Commis tool events (v2.1 Activity Ticker) =====
      case 'commis_tool_started': {
        const payload = wrapper.payload as CommisToolStartedPayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Commis tool started:', payload.tool_name);
        eventBus.emit('commis:tool_started', {
          commisId: payload.commis_id,
          toolName: payload.tool_name,
          toolCallId: payload.tool_call_id,
          argsPreview: payload.tool_args_preview,
          timestamp: Date.now(),
          courseId: payload.course_id ?? this.currentCourseId ?? undefined,
        });
        break;
      }

      case 'commis_tool_completed': {
        const payload = wrapper.payload as CommisToolCompletedPayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Commis tool completed:', payload.tool_name);
        eventBus.emit('commis:tool_completed', {
          commisId: payload.commis_id,
          toolName: payload.tool_name,
          toolCallId: payload.tool_call_id,
          durationMs: payload.duration_ms,
          resultPreview: payload.result_preview,
          timestamp: Date.now(),
          courseId: payload.course_id ?? this.currentCourseId ?? undefined,
        });
        break;
      }

      case 'commis_tool_failed': {
        const payload = wrapper.payload as CommisToolFailedPayload;
        this.petWatchdog();
        logger.warn(`[ConciergeChat] Commis tool failed: ${payload.tool_name} - ${payload.error}`);
        eventBus.emit('commis:tool_failed', {
          commisId: payload.commis_id,
          toolName: payload.tool_name,
          toolCallId: payload.tool_call_id,
          durationMs: payload.duration_ms,
          error: payload.error,
          timestamp: Date.now(),
          courseId: payload.course_id ?? this.currentCourseId ?? undefined,
        });
        break;
      }

      // ===== Concierge tool events (uniform treatment with commis tools) =====
      case 'concierge_tool_started': {
        const payload = wrapper.payload as ConciergeToolStartedPayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Concierge tool started:', payload.tool_name);
        eventBus.emit('concierge:tool_started', {
          courseId: payload.course_id ?? this.currentCourseId ?? 0,
          toolName: payload.tool_name,
          toolCallId: payload.tool_call_id,
          argsPreview: payload.tool_args_preview,
          args: payload.tool_args,
          timestamp: Date.now(),
        });
        break;
      }

      case 'concierge_tool_progress': {
        const payload = wrapper.payload as ConciergeToolProgressPayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Concierge tool progress:', payload.message);
        eventBus.emit('concierge:tool_progress', {
          courseId: payload.course_id ?? this.currentCourseId ?? 0,
          toolCallId: payload.tool_call_id,
          message: payload.message,
          level: payload.level,
          progressPct: payload.progress_pct,
          data: payload.data,
          timestamp: Date.now(),
        });
        break;
      }

      case 'concierge_tool_completed': {
        const payload = wrapper.payload as ConciergeToolCompletedPayload;
        this.petWatchdog();
        logger.debug('[ConciergeChat] Concierge tool completed:', payload.tool_name);
        eventBus.emit('concierge:tool_completed', {
          courseId: payload.course_id ?? this.currentCourseId ?? 0,
          toolName: payload.tool_name,
          toolCallId: payload.tool_call_id,
          durationMs: payload.duration_ms,
          resultPreview: payload.result_preview,
          result: payload.result,
          timestamp: Date.now(),
        });
        break;
      }

      case 'concierge_tool_failed': {
        const payload = wrapper.payload as ConciergeToolFailedPayload;
        this.petWatchdog();
        logger.warn(`[ConciergeChat] Concierge tool failed: ${payload.tool_name} - ${payload.error}`);
        eventBus.emit('concierge:tool_failed', {
          courseId: payload.course_id ?? this.currentCourseId ?? 0,
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
        logger.debug('[ConciergeChat] Unknown SSE event type:', { eventType, data });
    }
  }

  /**
   * Cancel current message stream
   */
  cancel(): void {
    if (this.currentAbortController) {
      logger.debug('[ConciergeChat] Cancelling current stream');
      this.currentAbortController.abort();
      this.currentAbortController = null;
    }

    if (this.currentCourseId) {
      eventBus.emit('concierge:cleared', { timestamp: Date.now() });
      this.currentCourseId = null;
    }
  }

  /**
   * Clear server-side conversation history
   * Creates a new Concierge thread, effectively clearing all history
   */
  async clearHistory(): Promise<void> {
    try {
      logger.debug('[ConciergeChat] Clearing server-side history...');

      const url = toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/history`);
      const response = await fetch(url, {
        method: 'DELETE',
        credentials: 'include', // Cookie auth
      });

      if (!response.ok) {
        throw new Error(`Failed to clear history: ${response.status} ${response.statusText}`);
      }

      logger.debug('[ConciergeChat] Server-side history cleared');
    } catch (_error) {
      logger.error('[ConciergeChat] Failed to clear history:', _error);
      throw _error;
    }
  }

  /**
   * Attach to an existing course's event stream
   * Used for reconnecting after page refresh (Resumable SSE v1)
   */
  async attachToCourse(courseId: number): Promise<void> {
    logger.debug(`[ConciergeChat] Attaching to course ${courseId} (lastEventId=${this.lastEventId})...`);

    // Cancel any current stream
    if (this.currentAbortController) {
      this.currentAbortController.abort();
    }

    // Create new abort controller
    this.currentAbortController = new AbortController();
    this.currentCourseId = courseId;

    try {
      // Use new resumable SSE endpoint: /api/stream/courses/{course_id}
      // This endpoint supports replay from lastEventId
      const url = new URL(toAbsoluteUrl(`/api/stream/courses/${courseId}`));

      // Add resumption parameter if we have a last event ID
      if (this.lastEventId > 0) {
        url.searchParams.set('after_event_id', String(this.lastEventId));
        logger.debug(`[ConciergeChat] Resuming from event ID: ${this.lastEventId}`);
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
        throw new Error(`Failed to attach to course: ${response.status} ${response.statusText}`);
      }

      const body = response.body;
      if (!body) {
        throw new Error('No response body for SSE stream');
      }

      // Start SSE processing in the background.
      //
      // Important: attachToCourse() must resolve quickly (on first SSE event) so the UI
      // can stop showing "reconnecting..." while the course is still active.
      const signal = this.currentAbortController.signal;
      const ready = new Promise<void>((resolve) => {
        this.onAnySseEventOnce = resolve;
      });

      void this.processSSEStream(body, signal)
        .then(() => {
          logger.debug(`[ConciergeChat] Attached to course ${courseId} and stream completed`);
        })
        .catch((error) => {
          if (error instanceof Error && error.name === 'AbortError') {
            logger.debug(`[ConciergeChat] Attach to course ${courseId} aborted`);
            return;
          }
          logger.error(`[ConciergeChat] SSE stream error while attached to course ${courseId}:`, error);
        })
        .finally(() => {
          if (this.currentCourseId === courseId) {
            this.currentAbortController = null;
            this.currentCourseId = null;
            this.onAnySseEventOnce = null;
          }
        });

      // Wait until the stream is actually live (first event/heartbeat), but don't block forever.
      await Promise.race([ready, new Promise<void>((resolve) => setTimeout(resolve, 1500))]);
    } catch (_error) {
      if (_error instanceof Error && _error.name === 'AbortError') {
        logger.debug(`[ConciergeChat] Attach to course ${courseId} aborted`);
        return;
      }

      logger.error(`[ConciergeChat] Failed to attach to course ${courseId}:`, _error);
      throw _error;
    }
  }

  /**
   * Clean up resources
   */
  dispose(): void {
    this.cancel();
    logger.debug('[ConciergeChat] Disposed');
  }
}
