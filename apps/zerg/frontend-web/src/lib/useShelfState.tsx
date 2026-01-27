import { createContext, useContext, useState, ReactNode, useEffect } from 'react';

/**
 * Shared mobile drawer state for Canvas fiche shelf and Chat thread sidebar.
 *
 * DESIGN NOTE: This uses a single shared `isShelfOpen` boolean for both the canvas
 * fiche shelf and chat thread sidebar. This means:
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
    // Guard against SSR/Node environments where localStorage doesn't exist
    if (typeof window === 'undefined' || typeof localStorage === 'undefined') {
      return false;
    }
    try {
      const stored = localStorage.getItem(SHELF_STORAGE_KEY);
      return stored ? JSON.parse(stored) : false;
    } catch {
      return false; // Fallback on storage error
    }
  });

  // Persist state to localStorage whenever it changes
  useEffect(() => {
    if (typeof localStorage === 'undefined') return;
    try {
      localStorage.setItem(SHELF_STORAGE_KEY, JSON.stringify(isShelfOpen));
    } catch (error) {
      console.warn('Failed to persist shelf state:', error);
    }
  }, [isShelfOpen]);

  // Close shelf when clicking outside on mobile (click-outside pattern)
  useEffect(() => {
    if (!isShelfOpen) return;

    const handleClickOutside = (event: MouseEvent | TouchEvent) => {
      const shelfElement = document.getElementById('fiche-shelf');
      const target = event.target as Node;

      // Close shelf if click is outside the shelf element
      if (shelfElement && !shelfElement.contains(target)) {
        setIsShelfOpen(false);
      }
    };

    // Use capture phase to intercept clicks early
    document.addEventListener('click', handleClickOutside, true);
    return () => {
      document.removeEventListener('click', handleClickOutside, true);
    };
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
