/**
 * SessionPickerProvider - Context provider for session picker modal
 *
 * Provides a promise-based API for opening the session picker and getting
 * the user's selection. Similar to ConfirmProvider pattern.
 *
 * Usage:
 * ```tsx
 * // In a component:
 * const { showSessionPicker } = useSessionPicker();
 *
 * // Open picker and get selection
 * const sessionId = await showSessionPicker({ project: "zerg" });
 * if (sessionId) {
 *   // User selected a session
 *   resumeSession(sessionId);
 * } else {
 *   // User cancelled
 * }
 * ```
 */

import { createContext, useContext, useState, useCallback, useRef, useEffect, type ReactNode } from "react";
import { SessionPickerModal } from "./SessionPickerModal";
import type { SessionFilters } from "../services/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ShowSessionPickerOptions {
  /** Pre-populate filters */
  filters?: SessionFilters;
  /** Whether to show "Start New Session" button */
  showStartNew?: boolean;
}

export interface SessionPickerResult {
  /** Selected session ID, or null if cancelled */
  sessionId: string | null;
  /** Whether user clicked "Start New" */
  startNew?: boolean;
}

type ShowPickerFn = (options?: ShowSessionPickerOptions) => Promise<SessionPickerResult>;

interface SessionPickerContextValue {
  showSessionPicker: ShowPickerFn;
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

const SessionPickerContext = createContext<SessionPickerContextValue | null>(null);

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * Hook to access the session picker.
 *
 * @throws Error if used outside of SessionPickerProvider
 */
export function useSessionPicker(): SessionPickerContextValue {
  const context = useContext(SessionPickerContext);
  if (!context) {
    throw new Error("useSessionPicker must be used within SessionPickerProvider");
  }
  return context;
}

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

interface ModalState {
  isOpen: boolean;
  filters?: SessionFilters;
  showStartNew?: boolean;
}

const initialState: ModalState = {
  isOpen: false,
};

interface SessionPickerProviderProps {
  children: ReactNode;
}

/**
 * Provider that renders a SessionPickerModal and exposes a promise-based
 * showSessionPicker() function via context.
 *
 * Wrap your app root with this provider:
 * ```tsx
 * <SessionPickerProvider>
 *   <App />
 * </SessionPickerProvider>
 * ```
 */
export function SessionPickerProvider({ children }: SessionPickerProviderProps) {
  const [state, setState] = useState<ModalState>(initialState);
  const resolveRef = useRef<((result: SessionPickerResult) => void) | null>(null);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      resolveRef.current?.({ sessionId: null });
      resolveRef.current = null;
    };
  }, []);

  // Promise-based API to show the picker
  const showSessionPicker = useCallback<ShowPickerFn>((options = {}) => {
    // If already open, reject with null
    if (resolveRef.current) {
      console.warn("[SessionPickerProvider] showSessionPicker() called while already open; returning null.");
      return Promise.resolve({ sessionId: null });
    }

    return new Promise<SessionPickerResult>((resolve) => {
      resolveRef.current = resolve;
      setState({
        isOpen: true,
        filters: options.filters,
        showStartNew: options.showStartNew,
      });
    });
  }, []);

  // Handle session selection
  const handleSelect = useCallback((sessionId: string) => {
    resolveRef.current?.({ sessionId });
    resolveRef.current = null;
    setState(initialState);
  }, []);

  // Handle cancel/close
  const handleClose = useCallback(() => {
    resolveRef.current?.({ sessionId: null });
    resolveRef.current = null;
    setState(initialState);
  }, []);

  // Handle "Start New" button
  const handleStartNew = useCallback(() => {
    resolveRef.current?.({ sessionId: null, startNew: true });
    resolveRef.current = null;
    setState(initialState);
  }, []);

  return (
    <SessionPickerContext.Provider value={{ showSessionPicker }}>
      {children}
      <SessionPickerModal
        isOpen={state.isOpen}
        initialFilters={state.filters}
        onClose={handleClose}
        onSelect={handleSelect}
        onStartNew={state.showStartNew ? handleStartNew : undefined}
      />
    </SessionPickerContext.Provider>
  );
}

export default SessionPickerProvider;
