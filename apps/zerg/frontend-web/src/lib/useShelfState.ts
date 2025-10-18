import { createContext, useContext, useState, ReactNode } from 'react';

interface ShelfContextType {
  isShelfOpen: boolean;
  toggleShelf: () => void;
  closeShelf: () => void;
}

export const ShelfContext = createContext<ShelfContextType | null>(null);

export function ShelfProvider({ children }: { children: ReactNode }) {
  const [isShelfOpen, setIsShelfOpen] = useState(false);

  const toggleShelf = () => setIsShelfOpen(prev => !prev);
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
