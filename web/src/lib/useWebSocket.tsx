import { useEffect, useRef, useCallback, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { toast } from 'react-hot-toast';
import { getWebSocketConfig } from './config';
import { useLatest } from '../hooks/useLatest';
import { RefreshUnavailableError, refreshAccessToken } from './auth-refresh';
import { replaceWithLoginUrl } from './loginRedirect';
import { requestNativeAuth } from './nativeAuthBridge';

// Maximum number of messages to queue when disconnected
// Prevents memory leak if user performs many actions while offline
const MAX_QUEUED_MESSAGES = 100;
const MAX_AUTH_RECOVERY_ATTEMPTS = 3;
const AUTH_READY_GRACE_MS = 3000;
const STREAMING_MESSAGE_TYPES = new Set([
  'stream_start',
  'stream_chunk',
  'stream_end',
  'assistant_id',
]);

export enum ConnectionStatus {
  DISCONNECTED = 'disconnected',
  CONNECTING = 'connecting',
  CONNECTED = 'connected',
  ERROR = 'error',
  RECONNECTING = 'reconnecting',
}

export interface WebSocketMessage {
  type: string;
  data?: unknown;
  [key: string]: unknown;
}

/**
 * Create an envelope-format message for the WS protocol.
 * All outbound messages must use this format.
 */
export function createEnvelope(
  type: string,
  topic: string,
  data: Record<string, unknown>,
  reqId?: string,
): WebSocketMessage {
  return {
    v: 1,
    type,
    topic,
    ts: Date.now(),
    data,
    ...(reqId != null ? { req_id: reqId } : {}),
  };
}

interface UseWebSocketOptions {
  // Authentication
  includeAuth?: boolean;

  // Reconnection settings
  reconnectInterval?: number;
  maxReconnectAttempts?: number;

  // Query invalidation
  invalidateQueries?: (string | number | object)[][];

  // Event handlers
  onMessage?: (message: WebSocketMessage) => void;
  onConnect?: () => void;
  onDisconnect?: () => void;
  onError?: (error: Event) => void;

  // Streaming message handler
  onStreamingMessage?: (envelope: WebSocketMessage) => void;

  // Connection lifecycle
  autoConnect?: boolean;
}

function subscriptionTopics(message: WebSocketMessage): string[] {
  if (message.type !== 'subscribe' && message.type !== 'unsubscribe') return [];
  const data = message.data;
  if (!data || typeof data !== 'object' || !('topics' in data)) return [];
  const topics = data.topics;
  return Array.isArray(topics) ? topics.filter((topic): topic is string => typeof topic === 'string' && topic.length > 0) : [];
}

interface UseWebSocketReturn {
  connectionStatus: ConnectionStatus;
  sendMessage: (message: WebSocketMessage) => void;
  connect: () => void;
  disconnect: () => void;
  reconnect: () => void;
}

function resolveWsBase(): string {
  if (typeof window === "undefined") {
    return "";
  }

  const wsConfig = getWebSocketConfig();

  // NO FALLBACKS: Config must be correct or we fail
  if (!wsConfig.baseUrl) {
    throw new Error('FATAL: WebSocket baseUrl not configured! Check config.js');
  }

  return wsConfig.baseUrl;
}

export function useWebSocket(
  enabled: boolean = true,
  options: UseWebSocketOptions = {}
): UseWebSocketReturn {
  const wsConfig = getWebSocketConfig();

  const {
    reconnectInterval = wsConfig.reconnectInterval,
    maxReconnectAttempts = wsConfig.maxReconnectAttempts,
    invalidateQueries = [],
    onMessage,
    onConnect,
    onDisconnect,
    onError,
    onStreamingMessage,
    autoConnect = true,
  } = options;

  const queryClient = useQueryClient();
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>(
    enabled && autoConnect ? ConnectionStatus.CONNECTING : ConnectionStatus.DISCONNECTED
  );

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimeoutRef = useRef<number | null>(null);
  const messageQueueRef = useRef<WebSocketMessage[]>([]);
  const authRecoveryPromiseRef = useRef<Promise<void> | null>(null);
  const authRecoveryAttemptsRef = useRef(0);
  const authReadyTimeoutRef = useRef<number | null>(null);
  const authenticatedRef = useRef(false);
  const connectRef = useRef<(() => void) | null>(null);
  const activeSubscriptionTopicsRef = useRef<Set<string>>(new Set());
  const hasAuthenticatedConnectionRef = useRef(false);
  const intentionalCloseRef = useRef(false);
  const latestOptionsRef = useLatest({
    invalidateQueries,
    onConnect,
    onDisconnect,
    onError,
    onMessage,
    onStreamingMessage,
  });

  const buildWebSocketUrl = useCallback(() => {
    const base = resolveWsBase().replace(/\/+$/, ''); // Strip trailing slashes
    const url = new URL("/api/ws", base);

    // Auth is handled via HttpOnly cookie (longhouse_session)
    // Cookies are automatically sent on WebSocket connections to same-origin
    // No need to pass token as query param anymore

    // Add test worker ID for E2E testing
    const workerId = window.__TEST_WORKER_ID__;
    if (workerId !== undefined) {
      url.searchParams.set("worker", String(workerId));
    }

    return url.toString();
  }, []);

  const clearAuthReadyTimeout = useCallback(() => {
    if (authReadyTimeoutRef.current !== null) {
      window.clearTimeout(authReadyTimeoutRef.current);
      authReadyTimeoutRef.current = null;
    }
  }, []);
  const markAuthenticated = useCallback(() => {
    clearAuthReadyTimeout();
    const isReconnect = hasAuthenticatedConnectionRef.current;
    authenticatedRef.current = true;
    hasAuthenticatedConnectionRef.current = true;
    authRecoveryAttemptsRef.current = 0;
    setConnectionStatus(ConnectionStatus.CONNECTED);
    reconnectAttemptsRef.current = 0;

    if (isReconnect && wsRef.current?.readyState === WebSocket.OPEN && activeSubscriptionTopicsRef.current.size > 0) {
      const resubscribe = createEnvelope(
        'subscribe',
        'system',
        { topics: Array.from(activeSubscriptionTopicsRef.current) },
        `resubscribe-${Date.now()}`,
      );
      wsRef.current.send(JSON.stringify(resubscribe));
    }

    if (wsRef.current && messageQueueRef.current.length > 0) {
      messageQueueRef.current.forEach(message => {
        wsRef.current?.send(JSON.stringify(message));
      });
      messageQueueRef.current = [];
    }

    latestOptionsRef.current.onConnect?.();
  }, [clearAuthReadyTimeout, latestOptionsRef]);


  const handleMessage = useCallback((event: MessageEvent) => {
    let message: WebSocketMessage;

    try {
      message = JSON.parse(event.data);
    } catch {
      // If not JSON, treat as simple message
      message = { type: 'message', data: event.data };
    }
    if (message.type === 'auth_ready') {
      markAuthenticated();
      return;
    }

    // Handle heartbeat protocol — respond with envelope-format pong
    if (message.type === 'ping') {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify(createEnvelope('pong', 'system', { timestamp: Date.now() })));
      }
      return;
    }

    // Check if this is a streaming message
    if (STREAMING_MESSAGE_TYPES.has(message.type)) {
      // Only log non-chunk messages to avoid noise (chunks logged with sampling in ChatPage)
      // if (message.type !== 'stream_chunk') {
      //   console.log('[WS] 🌊', message.type.toUpperCase());
      // }
      // Call streaming message handler if provided
      latestOptionsRef.current.onStreamingMessage?.(message);
    }

    // Call custom message handler if provided
    latestOptionsRef.current.onMessage?.(message);

    // Invalidate specified queries (but not for streaming chunks to avoid flicker)
    if (!STREAMING_MESSAGE_TYPES.has(message.type)) {
      latestOptionsRef.current.invalidateQueries.forEach(queryKey => {
        queryClient.invalidateQueries({ queryKey });
      });
    }
  }, [latestOptionsRef, markAuthenticated, queryClient]);

  const handleConnect = useCallback(() => {
    // New servers emit auth_ready only after validating the cookie and
    // registering the socket. The bounded fallback preserves compatibility
    // with older servers that authenticated before accept() but had no
    // auth_ready frame; it never runs after an explicit auth rejection.
    setConnectionStatus(ConnectionStatus.CONNECTING);
    clearAuthReadyTimeout();
    authReadyTimeoutRef.current = window.setTimeout(() => {
      authReadyTimeoutRef.current = null;
      if (!authenticatedRef.current && wsRef.current?.readyState === WebSocket.OPEN) {
        markAuthenticated();
      }
    }, AUTH_READY_GRACE_MS);
  }, [clearAuthReadyTimeout, markAuthenticated]);

  const scheduleReconnect = useCallback(() => {
    if (
      !enabled
      || !wsRef.current
      || reconnectTimeoutRef.current !== null
      || reconnectAttemptsRef.current >= maxReconnectAttempts
    ) {
      return;
    }
    setConnectionStatus(ConnectionStatus.RECONNECTING);
    const retryDelay = Math.min(
      reconnectInterval * Math.pow(2, reconnectAttemptsRef.current),
      30000,
    );
    reconnectTimeoutRef.current = window.setTimeout(() => {
      reconnectTimeoutRef.current = null;
      reconnectAttemptsRef.current++;
      connectRef.current?.();
    }, retryDelay);
  }, [enabled, maxReconnectAttempts, reconnectInterval]);

  const scheduleAuthReconnect = useCallback(() => {
    const attempt = authRecoveryAttemptsRef.current;
    if (
      !enabled
      || !wsRef.current
      || reconnectTimeoutRef.current !== null
      || attempt > MAX_AUTH_RECOVERY_ATTEMPTS
    ) {
      return;
    }
    const retryDelay = Math.min(
      reconnectInterval * Math.pow(2, Math.max(attempt - 1, 0)),
      30000,
    );
    setConnectionStatus(ConnectionStatus.RECONNECTING);
    reconnectTimeoutRef.current = window.setTimeout(() => {
      reconnectTimeoutRef.current = null;
      connectRef.current?.();
    }, retryDelay);
  }, [enabled, reconnectInterval]);

  const recoverAuthentication = useCallback(async () => {
    if (authRecoveryPromiseRef.current) {
      await authRecoveryPromiseRef.current;
      return;
    }

    const recovery = (async () => {
      try {
        const refreshed = await refreshAccessToken();
        if (refreshed) {
          if (!enabled || wsRef.current === null) return;
          authRecoveryAttemptsRef.current += 1;
          if (authRecoveryAttemptsRef.current > MAX_AUTH_RECOVERY_ATTEMPTS) {
            const returnTo = window.location.pathname + window.location.search + window.location.hash;
            if (!requestNativeAuth(returnTo)) {
              replaceWithLoginUrl(returnTo);
            }
            setConnectionStatus(ConnectionStatus.ERROR);
            return;
          }
          scheduleAuthReconnect();
          return;
        }

        const returnTo = window.location.pathname + window.location.search + window.location.hash;
        if (!requestNativeAuth(returnTo)) {
          replaceWithLoginUrl(returnTo);
        }
        setConnectionStatus(ConnectionStatus.ERROR);
      } catch (error) {
        if (!(error instanceof RefreshUnavailableError)) {
          throw error;
        }
        // The refresh authority is unavailable, not rejecting the user.
        // Keep the socket recoverable with the same bounded backoff as a
        // transport reconnect.
        scheduleReconnect();
      } finally {
        authRecoveryPromiseRef.current = null;
      }
    })();

    authRecoveryPromiseRef.current = recovery;
    await recovery;
  }, [enabled, scheduleAuthReconnect, scheduleReconnect]);

  const handleDisconnect = useCallback((event?: Event) => {
    clearAuthReadyTimeout();
    authenticatedRef.current = false;
    setConnectionStatus(ConnectionStatus.DISCONNECTED);
    latestOptionsRef.current.onDisconnect?.();

    // A 4401 is an expired browser access cookie, not a transport outage.
    // Refresh the HttpOnly session first; blindly reconnecting only repeats
    // rejected handshakes and makes a healthy user look disconnected.
    if ((event as CloseEvent | undefined)?.code === 4401) {
      setConnectionStatus(ConnectionStatus.RECONNECTING);
      void recoverAuthentication();
      return;
    }

    scheduleReconnect();
  }, [clearAuthReadyTimeout, latestOptionsRef, recoverAuthentication, scheduleReconnect]);

  const handleError = useCallback((error: Event) => {
    clearAuthReadyTimeout();
    // Skip self-inflicted errors (StrictMode cleanup during handshake)
    if (intentionalCloseRef.current) {
      intentionalCloseRef.current = false;
      return;
    }

    console.error('[WS] ❌ WebSocket error:', error);
    setConnectionStatus(ConnectionStatus.ERROR);
    latestOptionsRef.current.onError?.(error);

    if (reconnectAttemptsRef.current === 0) {
      toast.error("WebSocket connection failed. Real-time features disabled.", { duration: 5000 });
    } else if (reconnectAttemptsRef.current < maxReconnectAttempts) {
      toast.error("Connection lost. Attempting to reconnect...", { duration: 3000 });
    }
  }, [clearAuthReadyTimeout, latestOptionsRef, maxReconnectAttempts]);

  const connect = useCallback(() => {
    clearAuthReadyTimeout();
    // Clean up existing connection
    if (wsRef.current) {
      const existingSocket = wsRef.current;
      try {
        // Mark as intentional so handleError ignores self-inflicted closure
        // (e.g. StrictMode effect re-run or manual reconnect).
        intentionalCloseRef.current = true;
        if (typeof existingSocket.removeEventListener === 'function') {
          existingSocket.removeEventListener('message', handleMessage);
          existingSocket.removeEventListener('open', handleConnect);
          existingSocket.removeEventListener('close', handleDisconnect);
          existingSocket.removeEventListener('error', handleError);
        } else {
          existingSocket.onmessage = null;
          existingSocket.onopen = null;
          existingSocket.onclose = null;
          existingSocket.onerror = null;
        }
        existingSocket.close();
      } catch (error) {
        // Ignore cleanup errors in test environment
        console.warn('WebSocket cleanup error:', error);
      } finally {
        intentionalCloseRef.current = false;
      }
    }

    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    if (!enabled) {
      setConnectionStatus(ConnectionStatus.DISCONNECTED);
      return;
    }

    try {
      authenticatedRef.current = false;
      setConnectionStatus(ConnectionStatus.CONNECTING);
      const wsUrl = buildWebSocketUrl();
      // console.log('[WS] 🔌 Attempting to connect to:', wsUrl);
      wsRef.current = new WebSocket(wsUrl);

      if (typeof wsRef.current.addEventListener === 'function') {
        wsRef.current.addEventListener('message', handleMessage);
        wsRef.current.addEventListener('open', handleConnect);
        wsRef.current.addEventListener('close', handleDisconnect);
        wsRef.current.addEventListener('error', handleError);
      } else {
        // LEGACY FALLBACK: Required for test mocks that don't implement addEventListener
        // See: frontend-web/src/pages/__tests__/AutomationsPage.test.tsx:67
        // Our test suite stubs WebSocket with only onmessage/onopen/etc properties
        // DO NOT REMOVE without updating test infrastructure
        wsRef.current.onmessage = handleMessage as EventListener;
        wsRef.current.onopen = handleConnect as EventListener;
        wsRef.current.onclose = handleDisconnect as EventListener;
        wsRef.current.onerror = handleError as EventListener;
      }
    } catch (error) {
      setConnectionStatus(ConnectionStatus.ERROR);
      console.error('Failed to create WebSocket connection:', error);
    }
  }, [clearAuthReadyTimeout, enabled, buildWebSocketUrl, handleMessage, handleConnect, handleDisconnect, handleError]);

  // Store connect function in ref to avoid circular dependencies
  connectRef.current = connect;

  const disconnect = useCallback(() => {
    clearAuthReadyTimeout();
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    if (wsRef.current) {
      const existingSocket = wsRef.current;
      // Mark as intentional so handleError ignores self-inflicted closure
      intentionalCloseRef.current = true;

      try {
        if (typeof existingSocket.removeEventListener === 'function') {
          existingSocket.removeEventListener('message', handleMessage);
          existingSocket.removeEventListener('open', handleConnect);
          existingSocket.removeEventListener('close', handleDisconnect);
          existingSocket.removeEventListener('error', handleError);
        } else {
          existingSocket.onmessage = null;
          existingSocket.onopen = null;
          existingSocket.onclose = null;
          existingSocket.onerror = null;
        }
        existingSocket.close();
        wsRef.current = null;
      } catch (error) {
        console.warn('WebSocket disconnect error:', error);
        wsRef.current = null;
      } finally {
        intentionalCloseRef.current = false;
      }
    }

    setConnectionStatus(ConnectionStatus.DISCONNECTED);
  }, [clearAuthReadyTimeout, handleMessage, handleConnect, handleDisconnect, handleError]);

  const reconnect = useCallback(() => {
    reconnectAttemptsRef.current = 0;
    connect();
  }, [connect]);

  const sendMessage = useCallback((message: WebSocketMessage) => {
    const topics = subscriptionTopics(message);
    if (topics.length > 0) {
      if (message.type === 'subscribe') {
        topics.forEach(topic => activeSubscriptionTopicsRef.current.add(topic));
      } else {
        topics.forEach(topic => activeSubscriptionTopicsRef.current.delete(topic));
      }
    }

    if (authenticatedRef.current && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(message));
    } else {
      // Queue message if not connected, but enforce bounds to prevent memory leak
      if (messageQueueRef.current.length >= MAX_QUEUED_MESSAGES) {
        console.warn(
          `[WS] Message queue full (${MAX_QUEUED_MESSAGES} messages). Dropping oldest message.`
        );
        // Remove oldest message (FIFO)
        messageQueueRef.current.shift();
      }
      messageQueueRef.current.push(message);

      // Try to connect if not already connecting
      if (connectionStatus === ConnectionStatus.DISCONNECTED) {
        connect();
      }
    }
  }, [connectionStatus, connect]);

  // Effect to manage connection lifecycle
  useEffect(() => {
    if (enabled && autoConnect) {
      connect();
    }

    return () => {
      disconnect();
    };
  }, [enabled, autoConnect, connect, disconnect]);
  useEffect(() => {
    const handleAuthLogout = () => disconnect();
    window.addEventListener('longhouse-auth-logout', handleAuthLogout);
    return () => window.removeEventListener('longhouse-auth-logout', handleAuthLogout);
  }, [disconnect]);

  // Expose sendMessage for E2E testing of queue behavior
  // This allows tests to directly call sendMessage to test queue bounds
  // Only active when __TEST_WORKER_ID__ is set by Playwright fixtures
  useEffect(() => {
    if (typeof window !== 'undefined' && window.__TEST_WORKER_ID__ !== undefined) {
      window.__testSendMessage = sendMessage;
    }
    // Cleanup when component unmounts
    return () => {
      if (typeof window !== 'undefined') {
        delete window.__testSendMessage;
      }
    };
  }, [sendMessage]);

  return {
    connectionStatus,
    sendMessage,
    connect,
    disconnect,
    reconnect,
  };
}

// Connection status indicator component
interface ConnectionStatusIndicatorProps {
  status: ConnectionStatus;
  showText?: boolean;
}

export function ConnectionStatusIndicator({
  status,
  showText = true
}: ConnectionStatusIndicatorProps) {
  const getStatusColor = () => {
    switch (status) {
      case ConnectionStatus.CONNECTED:
        return '#5D9B4A'; // olive
      case ConnectionStatus.CONNECTING:
      case ConnectionStatus.RECONNECTING:
        return '#D4A843'; // warm amber
      case ConnectionStatus.ERROR:
        return '#C45040'; // warm red
      case ConnectionStatus.DISCONNECTED:
      default:
        return '#8A7A64'; // muted
    }
  };

  const getStatusText = () => {
    switch (status) {
      case ConnectionStatus.CONNECTED:
        return 'Connected';
      case ConnectionStatus.CONNECTING:
        return 'Connecting...';
      case ConnectionStatus.RECONNECTING:
        return 'Reconnecting...';
      case ConnectionStatus.ERROR:
        return 'Connection Error';
      case ConnectionStatus.DISCONNECTED:
      default:
        return 'Disconnected';
    }
  };

  return (
    <span
      data-ws-status={status}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
        fontSize: '0.875rem',
      }}
    >
      <span
        style={{
          width: '8px',
          height: '8px',
          borderRadius: '50%',
          backgroundColor: getStatusColor(),
          display: 'inline-block',
        }}
      />
      {showText && <span>{getStatusText()}</span>}
    </span>
  );
}
