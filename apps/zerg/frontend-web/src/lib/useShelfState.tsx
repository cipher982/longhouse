import { createContext, useContext, useState, ReactNode, useEffect } from 'react';

/**
 * Shared mobile drawer state for Canvas agent shelf and Chat thread sidebar.
 *
 * DESIGN NOTE: This uses a single shared `isShelfOpen` boolean for both the canvas
 * agent shelf and chat thread sidebar. This means:
 *
 * - Both drawers toggle together (opening canvas drawer closes chat drawer)
 * - Single hamburger button controls both on mobile
 * - Simplifies state management (one source of truth)
 *
 * Future: If independent drawer states are needed (canvas drawer open while chat
 * drawer closed), refactor to:
 *   isCanvasShelfOpen: boolean
 *   isChatSidebarOpen: boolean
 * And update CanvasPage/ChatPage to use separate hooks or state keys.
 *
 * Context: The two pages (CanvasPage, ChatPage) are rendered via React Router
 * in exclusive routes, so sharing state doesn't cause both drawers to appear
 * simultaneously (only one page renders at a time).
 */

interface ShelfContextType {
  isShelfOpen: boolean;
  toggleShelf: () => void;
  closeShelf: () => void;
}

export const ShelfContext = createContext<ShelfContextType | null>(null);

const SHELF_STORAGE_KEY = 'zerg:shelf-state';

export function ShelfProvider({ children }: { children: ReactNode }) {
  // Initialize from localStorage, default to false
  const [isShelfOpen, setIsShelfOpen] = useState(() => {
    try {
      const stored = localStorage.getItem(SHELF_STORAGE_KEY);
      return stored ? JSON.parse(stored) : false;
    } catch {
      return false; // Fallback on storage error
    }
  });

  // Persist state to localStorage whenever it changes
  useEffect(() => {
    try {
      localStorage.setItem(SHELF_STORAGE_KEY, JSON.stringify(isShelfOpen));
    } catch (error) {
      console.warn('Failed to persist shelf state:', error);
    }
  }, [isShelfOpen]);

  const toggleShelf = () => setIsShelfOpen((prev: boolean) => !prev);
  const closeShelf = () => setIsShelfOpen(false);

  return (
    <ShelfContext.Provider value={{ isShelfOpen, toggleShelf, closeShelf }}>
      {children}
    </ShelfContext.Provider>
  );
}

export function useShelf() {
  const context = useContext(ShelfContext);
  if (!context) {
    throw new Error('useShelf must be used within ShelfProvider');
  }
  return context;
}
