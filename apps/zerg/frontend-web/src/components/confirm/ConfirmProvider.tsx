import { createContext, useState, useCallback, useEffect, useRef, type ReactNode } from 'react';
import { ConfirmDialog } from './ConfirmDialog';
import type { ConfirmOptions } from './types';

type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>;

interface ConfirmContextValue {
  confirm: ConfirmFn;
}

// eslint-disable-next-line react-refresh/only-export-components
export const ConfirmContext = createContext<ConfirmContextValue | null>(null);

interface DialogState {
  isOpen: boolean;
  title: string;
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  variant: 'default' | 'danger' | 'warning';
}

const initialState: DialogState = {
  isOpen: false,
  title: '',
  message: '',
  confirmLabel: 'Confirm',
  cancelLabel: 'Cancel',
  variant: 'default',
};

interface ConfirmProviderProps {
  children: ReactNode;
}

/**
 * Provider that renders a single ConfirmDialog instance and exposes
 * a promise-based confirm() function via context.
 *
 * Wrap your app root with this provider:
 * ```tsx
 * <ConfirmProvider>
 *   <App />
 * </ConfirmProvider>
 * ```
 */
export function ConfirmProvider({ children }: ConfirmProviderProps) {
  const [state, setState] = useState<DialogState>(initialState);
  const resolveRef = useRef<((value: boolean) => void) | null>(null);

  useEffect(() => {
    return () => {
      resolveRef.current?.(false);
      resolveRef.current = null;
    };
  }, []);

  const confirm = useCallback<ConfirmFn>((options) => {
    if (resolveRef.current) {
      console.warn('[ConfirmProvider] confirm() called while another confirm is open; returning false.');
      return Promise.resolve(false);
    }

    return new Promise<boolean>((resolve) => {
      resolveRef.current = resolve;
      setState({
        isOpen: true,
        title: options.title,
        message: options.message,
        confirmLabel: options.confirmLabel ?? 'Confirm',
        cancelLabel: options.cancelLabel ?? 'Cancel',
        variant: options.variant ?? 'default',
      });
    });
  }, []);

  const handleConfirm = useCallback(() => {
    resolveRef.current?.(true);
    resolveRef.current = null;
    setState(initialState);
  }, []);

  const handleCancel = useCallback(() => {
    resolveRef.current?.(false);
    resolveRef.current = null;
    setState(initialState);
  }, []);

  return (
    <ConfirmContext.Provider value={{ confirm }}>
      {children}
      <ConfirmDialog
        isOpen={state.isOpen}
        title={state.title}
        message={state.message}
        confirmLabel={state.confirmLabel ?? 'Confirm'}
        cancelLabel={state.cancelLabel ?? 'Cancel'}
        variant={state.variant ?? 'default'}
        onConfirm={handleConfirm}
        onCancel={handleCancel}
      />
    </ConfirmContext.Provider>
  );
}
