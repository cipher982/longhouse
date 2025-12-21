export const SNAP_GRID_SIZE = 24;

// Simple debounce utility function
export function debounce<T extends unknown[]>(
  func: (...args: T) => void,
  wait: number
): (...args: T) => void {
  let timeout: number | undefined;
  return (...args: T) => {
    clearTimeout(timeout);
    timeout = window.setTimeout(() => func(...args), wait);
  };
}
