import { useState, useEffect, useCallback, useRef } from 'react';
import config from './config';

export type ServiceStatus = 'checking' | 'available' | 'unavailable' | 'error';

interface ServiceHealthState {
  status: ServiceStatus;
  lastCheck: Date | null;
  retryCount: number;
  error?: string;
}

/**
 * Detects if an error indicates the service is unavailable (502/503/network error)
 * vs a real application error (4xx, 5xx other than 502/503).
 */
export function isServiceUnavailable(error: unknown): boolean {
  // Network errors (CORS failures during 502, fetch failures)
  if (error instanceof TypeError && error.message.includes('fetch')) {
    return true;
  }

  // Check for Response objects or errors with status
  if (error && typeof error === 'object' && 'status' in error) {
    const status = (error as { status: number }).status;
    return status === 502 || status === 503 || status === 504;
  }

  // Check error message for common patterns
  if (error instanceof Error) {
    const msg = error.message.toLowerCase();
    if (
      msg.includes('502') ||
      msg.includes('503') ||
      msg.includes('504') ||
      msg.includes('bad gateway') ||
      msg.includes('service unavailable') ||
      msg.includes('gateway timeout') ||
      msg.includes('failed to fetch') ||
      msg.includes('network') ||
      msg.includes('cors')
    ) {
      return true;
    }
  }

  return false;
}

/**
 * Hook to track backend service availability.
 *
 * Returns the current service status and whether the service is available.
 * Automatically retries with exponential backoff when service is unavailable.
 */
export function useServiceHealth() {
  const [state, setState] = useState<ServiceHealthState>({
    status: 'checking',
    lastCheck: null,
    retryCount: 0,
  });

  const retryTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  const checkHealth = useCallback(async () => {
    if (!mountedRef.current) return;

    try {
      // Use the health endpoint - it's lightweight and doesn't require auth
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5000);

      const response = await fetch(`${config.apiBaseUrl}/health`, {
        method: 'GET',
        signal: controller.signal,
      });

      clearTimeout(timeoutId);

      if (!mountedRef.current) return;

      if (response.ok) {
        setState({
          status: 'available',
          lastCheck: new Date(),
          retryCount: 0,
        });
      } else if (response.status === 502 || response.status === 503 || response.status === 504) {
        setState(prev => ({
          status: 'unavailable',
          lastCheck: new Date(),
          retryCount: prev.retryCount + 1,
          error: `Service returned ${response.status}`,
        }));
      } else {
        // Other errors (4xx, other 5xx) - service is reachable but erroring
        setState({
          status: 'error',
          lastCheck: new Date(),
          retryCount: 0,
          error: `Unexpected status ${response.status}`,
        });
      }
    } catch (err) {
      if (!mountedRef.current) return;

      if (isServiceUnavailable(err)) {
        setState(prev => ({
          status: 'unavailable',
          lastCheck: new Date(),
          retryCount: prev.retryCount + 1,
          error: err instanceof Error ? err.message : 'Service unavailable',
        }));
      } else {
        setState({
          status: 'error',
          lastCheck: new Date(),
          retryCount: 0,
          error: err instanceof Error ? err.message : 'Unknown error',
        });
      }
    }
  }, []);

  // Schedule retry with exponential backoff
  useEffect(() => {
    if (state.status === 'unavailable') {
      // Exponential backoff: 1s, 2s, 4s, 8s, max 15s
      const delay = Math.min(1000 * Math.pow(2, state.retryCount - 1), 15000);

      retryTimeoutRef.current = setTimeout(() => {
        if (mountedRef.current) {
          checkHealth();
        }
      }, delay);

      return () => {
        if (retryTimeoutRef.current) {
          clearTimeout(retryTimeoutRef.current);
        }
      };
    }
  }, [state.status, state.retryCount, checkHealth]);

  // Initial check on mount
  useEffect(() => {
    mountedRef.current = true;
    checkHealth();

    return () => {
      mountedRef.current = false;
      if (retryTimeoutRef.current) {
        clearTimeout(retryTimeoutRef.current);
      }
    };
  }, [checkHealth]);

  return {
    status: state.status,
    isAvailable: state.status === 'available',
    isUnavailable: state.status === 'unavailable',
    isChecking: state.status === 'checking',
    retryCount: state.retryCount,
    error: state.error,
    retry: checkHealth,
  };
}
