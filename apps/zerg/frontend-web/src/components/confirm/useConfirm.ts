import { useContext } from 'react';
import { ConfirmContext } from './ConfirmProvider';

/**
 * Hook to access the confirm() function.
 *
 * Usage:
 * ```tsx
 * const confirm = useConfirm();
 *
 * const handleDelete = async () => {
 *   const ok = await confirm({
 *     title: 'Delete agent?',
 *     message: 'This cannot be undone.',
 *     confirmLabel: 'Delete',
 *     cancelLabel: 'Keep',
 *     variant: 'danger',
 *   });
 *   if (ok) await deleteAgent();
 * };
 * ```
 */
export function useConfirm() {
  const context = useContext(ConfirmContext);
  if (!context) {
    throw new Error('useConfirm must be used within a ConfirmProvider');
  }
  return context.confirm;
}
