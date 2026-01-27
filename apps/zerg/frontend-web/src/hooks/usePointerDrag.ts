import { useCallback, useRef } from 'react';

/**
 * Cross-platform drag handler supporting mouse and touch events.
 * Replaces HTML5 dragstart/dragover/drop for better mobile support.
 *
 * Usage:
 *   const { startDrag, getDragData, isDragging } = usePointerDrag();
 *
 *   // On item:
 *   onPointerDown={(e) => startDrag(e, { type: 'fiche', id: '123', name: 'Bot' })}
 *
 *   // On drop zone:
 *   onPointerUp={(e) => {
 *     const data = getDragData();
 *     if (data) handleDrop(data, e);
 *   }}
 */

export interface DragData {
  type: 'fiche' | 'tool';
  id?: string;
  name: string;
  [key: string]: unknown;
}

interface DragState {
  isActive: boolean;
  data: DragData | null;
  startX: number;
  startY: number;
  currentX: number;
  currentY: number;
  element: HTMLElement | null;
}

const DRAG_THRESHOLD = 5; // pixels to move before considering it a drag

export function usePointerDrag() {
  const dragState = useRef<DragState>({
    isActive: false,
    data: null,
    startX: 0,
    startY: 0,
    currentX: 0,
    currentY: 0,
    element: null,
  });

  /**
   * Start a drag operation from pointer down
   */
  const startDrag = useCallback((event: React.PointerEvent, data: DragData) => {
    // Only respond to primary pointer (left mouse, first touch)
    if (event.button !== undefined && event.button !== 0) return;
    if (event.isPrimary === false) return;

    dragState.current = {
      isActive: false, // Not considered dragging yet (need threshold)
      data,
      startX: event.clientX,
      startY: event.clientY,
      currentX: event.clientX,
      currentY: event.clientY,
      element: event.currentTarget as HTMLElement,
    };

    // Set capture on the element so we get all pointer events
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  }, []);

  /**
   * Update drag position on pointer move
   */
  const updateDragPosition = useCallback((event: PointerEvent) => {
    const state = dragState.current;
    if (!state.data) return;

    state.currentX = event.clientX;
    state.currentY = event.clientY;

    // Check if we've exceeded the drag threshold
    if (!state.isActive) {
      const dx = state.currentX - state.startX;
      const dy = state.currentY - state.startY;
      const distance = Math.sqrt(dx * dx + dy * dy);

      if (distance > DRAG_THRESHOLD) {
        state.isActive = true;
      }
    }
  }, []);

  /**
   * End the drag operation
   */
  const endDrag = useCallback((event: PointerEvent) => {
    const state = dragState.current;
    if (state.element) {
      try {
        state.element.releasePointerCapture(event.pointerId);
      } catch {
        // Pointer was already released
      }
    }

    dragState.current = {
      isActive: false,
      data: null,
      startX: 0,
      startY: 0,
      currentX: 0,
      currentY: 0,
      element: null,
    };
  }, []);

  /**
   * Get current drag data (if actively dragging)
   */
  const getDragData = useCallback((): DragData | null => {
    const state = dragState.current;
    return state.isActive ? state.data : null;
  }, []);

  /**
   * Get current drag position (for preview)
   */
  const getDragPosition = useCallback(() => {
    const state = dragState.current;
    return {
      x: state.currentX,
      y: state.currentY,
      startX: state.startX,
      startY: state.startY,
      isActive: state.isActive,
    };
  }, []);

  /**
   * Check if currently dragging
   */
  const isDragging = useCallback((): boolean => {
    return dragState.current.isActive;
  }, []);

  return {
    startDrag,
    updateDragPosition,
    endDrag,
    getDragData,
    getDragPosition,
    isDragging,
  };
}
