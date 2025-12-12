import { useEffect, useRef, useCallback, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { toast } from 'react-hot-toast';
import { getWebSocketConfig } from './config';

// Maximum number of messages to queue when disconnected
// Prevents memory leak if user performs many actions while offline
const MAX_QUEUED_MESSAGES = 100;

export enum ConnectionStatus {
  DISCONNECTED = 'disconnected',
  CONNECTING = 'connecting',
  CONNECTED = 'connected',
  ERROR = 'error',
  RECONNECTING = 'reconnecting',
}

interface WebSocketMessage {
  type: string;
  data?: unknown;
  [key: string]: unknown;
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
    includeAuth = wsConfig.includeAuth,
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
  const connectRef = useRef<(() => void) | null>(null);
  const intentionalCloseRef = useRef(false);
  const onMessageRef = useRef<typeof onMessage>();
  const onConnectRef = useRef<typeof onConnect>();
  const onDisconnectRef = useRef<typeof onDisconnect>();
  const onErrorRef = useRef<typeof onError>();
  const onStreamingMessageRef = useRef<typeof onStreamingMessage>();
  const invalidateQueriesRef = useRef<(string | number | object)[][]>(invalidateQueries);

  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

  useEffect(() => {
    onConnectRef.current = onConnect;
  }, [onConnect]);

  useEffect(() => {
    onDisconnectRef.current = onDisconnect;
  }, [onDisconnect]);

  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  useEffect(() => {
    onStreamingMessageRef.current = onStreamingMessage;
  }, [onStreamingMessage]);

  useEffect(() => {
    invalidateQueriesRef.current = invalidateQueries;
  }, [invalidateQueries]);

  const buildWebSocketUrl = useCallback(() => {
    const base = resolveWsBase();
    const url = new URL("/api/ws", base);

    // Auth is handled via HttpOnly cookie (swarmlet_session)
    // Cookies are automatically sent on WebSocket connections to same-origin
    // No need to pass token as query param anymore

    // Add test worker ID for E2E testing
    const workerId = (window as typeof window & { __TEST_WORKER_ID__?: string }).__TEST_WORKER_ID__;
    if (workerId !== undefined) {
      url.searchParams.set("worker", String(workerId));
    }

    return url.toString();
  }, []);

  const handleMessage = useCallback((event: MessageEvent) => {
    let message: WebSocketMessage;

    try {
      message = JSON.parse(event.data);
    } catch {
      // If not JSON, treat as simple message
      message = { type: 'message', data: event.data };
    }

    // Check if this is a streaming message
    const streamingTypes = [
      'stream_start', 'stream_chunk', 'stream_end', 'assistant_id',
      // Workflow execution events
      'execution_started', 'node_state', 'workflow_progress', 'execution_finished'
    ];
    if (streamingTypes.includes(message.type)) {
      // Only log non-chunk messages to avoid noise (chunks logged with sampling in ChatPage)
      // if (message.type !== 'stream_chunk') {
      //   console.log('[WS] 🌊', message.type.toUpperCase());
      // }
      // Call streaming message handler if provided
      onStreamingMessageRef.current?.(message);
    }

    // Call custom message handler if provided
    onMessageRef.current?.(message);

    // Invalidate specified queries (but not for streaming chunks to avoid flicker)
    if (!streamingTypes.includes(message.type)) {
      invalidateQueriesRef.current.forEach(queryKey => {
        queryClient.invalidateQueries({ queryKey });
      });
    }
  }, [queryClient]);

  const handleConnect = useCallback(() => {
    // console.log('[WS] ✅ WebSocket connected successfully');
    setConnectionStatus(ConnectionStatus.CONNECTED);
    reconnectAttemptsRef.current = 0;

    // Send any queued messages
    if (wsRef.current && messageQueueRef.current.length > 0) {
      // console.log('[WS] 📬 Sending', messageQueueRef.current.length, 'queued messages');
      messageQueueRef.current.forEach(message => {
        wsRef.current?.send(JSON.stringify(message));
      });
      messageQueueRef.current = [];
    }

    onConnectRef.current?.();
  }, []);

  const handleDisconnect = useCallback(() => {
    setConnectionStatus(ConnectionStatus.DISCONNECTED);
    onDisconnectRef.current?.();

    // Attempt reconnection if enabled and we haven't exceeded max attempts
    if (enabled && reconnectAttemptsRef.current < maxReconnectAttempts) {
      setConnectionStatus(ConnectionStatus.RECONNECTING);
      reconnectTimeoutRef.current = window.setTimeout(() => {
        reconnectAttemptsRef.current++;
        connectRef.current?.();
      }, reconnectInterval);
    }
  }, [enabled, maxReconnectAttempts, reconnectInterval]);

  const handleError = useCallback((error: Event) => {
    // Skip self-inflicted errors (StrictMode cleanup during handshake)
    if (intentionalCloseRef.current) {
      intentionalCloseRef.current = false;
      return;
    }

    console.error('[WS] ❌ WebSocket error:', error);
    setConnectionStatus(ConnectionStatus.ERROR);
    onErrorRef.current?.(error);

    if (reconnectAttemptsRef.current === 0) {
      toast.error("WebSocket connection failed. Real-time features disabled.", { duration: 5000 });
    } else if (reconnectAttemptsRef.current < maxReconnectAttempts) {
      toast.error("Connection lost. Attempting to reconnect...", { duration: 3000 });
    }
  }, [maxReconnectAttempts]);

  const connect = useCallback(() => {
    // Clean up existing connection
    if (wsRef.current) {
      try {
        if (typeof wsRef.current.removeEventListener === 'function') {
          wsRef.current.removeEventListener('message', handleMessage);
          wsRef.current.removeEventListener('open', handleConnect);
          wsRef.current.removeEventListener('close', handleDisconnect);
          wsRef.current.removeEventListener('error', handleError);
        }
        wsRef.current.close();
      } catch (error) {
        // Ignore cleanup errors in test environment
        console.warn('WebSocket cleanup error:', error);
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
        // See: frontend-web/src/pages/__tests__/DashboardPage.test.tsx:67
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
  }, [enabled, buildWebSocketUrl, handleMessage, handleConnect, handleDisconnect, handleError]);

  // Store connect function in ref to avoid circular dependencies
  connectRef.current = connect;

  const disconnect = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    if (wsRef.current) {
      // Mark as intentional so handleError ignores self-inflicted closure
      intentionalCloseRef.current = true;

      try {
        if (typeof wsRef.current.removeEventListener === 'function') {
          wsRef.current.removeEventListener('message', handleMessage);
          wsRef.current.removeEventListener('open', handleConnect);
          wsRef.current.removeEventListener('close', handleDisconnect);
          wsRef.current.removeEventListener('error', handleError);
        }
        wsRef.current.close();
        wsRef.current = null;
      } catch (error) {
        console.warn('WebSocket disconnect error:', error);
        wsRef.current = null;
        intentionalCloseRef.current = false;
      }
    }

    setConnectionStatus(ConnectionStatus.DISCONNECTED);
  }, [handleMessage, handleConnect, handleDisconnect, handleError]);

  const reconnect = useCallback(() => {
    reconnectAttemptsRef.current = 0;
    connect();
  }, [connect]);

  const sendMessage = useCallback((message: WebSocketMessage) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
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

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      disconnect();
    };
  }, [disconnect]);

  // Expose sendMessage for E2E testing of queue behavior
  // This allows tests to directly call sendMessage to test queue bounds
  // Only active when __TEST_WORKER_ID__ is set by Playwright fixtures
  useEffect(() => {
    if (typeof window !== 'undefined' && (window as any).__TEST_WORKER_ID__ !== undefined) {
      (window as any).__testSendMessage = sendMessage;
    }
    // Cleanup when component unmounts
    return () => {
      if (typeof window !== 'undefined') {
        delete (window as any).__testSendMessage;
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
        return '#10b981'; // green
      case ConnectionStatus.CONNECTING:
      case ConnectionStatus.RECONNECTING:
        return '#f59e0b'; // yellow
      case ConnectionStatus.ERROR:
        return '#ef4444'; // red
      case ConnectionStatus.DISCONNECTED:
      default:
        return '#6b7280'; // gray
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
    <span style={{
      display: 'flex',
      alignItems: 'center',
      gap: '6px',
      fontSize: '0.875rem',
    }}>
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
