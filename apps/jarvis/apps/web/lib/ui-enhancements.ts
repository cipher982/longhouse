/**
 * UI Enhancement Module - Modern visual feedback and animations
 * Styles are defined in index.css under "UI Enhancement Components"
 */

export class UIEnhancements {
  private toastContainer: HTMLDivElement | null = null;

  constructor() {
    this.initToastContainer();
  }

  /**
   * Initialize toast notification container
   */
  private initToastContainer(): void {
    this.toastContainer = document.createElement('div');
    this.toastContainer.className = 'toast-container';
    document.body.appendChild(this.toastContainer);
  }

  /**
   * Show toast notification
   */
  showToast(message: string, type: 'success' | 'error' | 'info' = 'info'): void {
    if (!this.toastContainer) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;

    const icons = {
      success: '✓',
      error: '✕',
      info: 'ⓘ'
    };

    toast.innerHTML = `
      <span class="toast-icon">${icons[type]}</span>
      <span>${message}</span>
    `;

    this.toastContainer.appendChild(toast);

    // Auto-remove after 4 seconds
    setTimeout(() => {
      toast.classList.add('toast-exit');
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  }

  /**
   * Show loading overlay
   */
  showLoading(message = 'Loading...'): HTMLDivElement {
    const overlay = document.createElement('div');
    overlay.className = 'loading-overlay';

    overlay.innerHTML = `
      <div class="loading-spinner"></div>
      <div class="loading-text">${message}</div>
    `;

    document.body.appendChild(overlay);
    return overlay;
  }

  /**
   * Hide loading overlay
   */
  hideLoading(overlay: HTMLDivElement): void {
    overlay.classList.add('loading-overlay-exit');
    setTimeout(() => overlay.remove(), 300);
  }

  /**
   * Animate element entrance
   */
  animateIn(element: HTMLElement, animation = 'slideIn'): void {
    element.style.animation = `${animation} 0.3s ease`;
  }

  /**
   * Add ripple effect to button
   */
  addRipple(button: HTMLElement, event: PointerEvent): void {
    const ripple = document.createElement('span');
    const rect = button.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height);
    const x = event.clientX - rect.left - size / 2;
    const y = event.clientY - rect.top - size / 2;

    ripple.className = 'ripple';
    ripple.style.width = `${size}px`;
    ripple.style.height = `${size}px`;
    ripple.style.left = `${x}px`;
    ripple.style.top = `${y}px`;

    button.classList.add('ripple-container');
    button.appendChild(ripple);

    setTimeout(() => ripple.remove(), 600);
  }

  /**
   * Create skeleton loader
   */
  createSkeleton(container: HTMLElement, lines = 3): void {
    container.innerHTML = '';
    for (let i = 0; i < lines; i++) {
      const skeleton = document.createElement('div');
      skeleton.className = 'skeleton-line';
      // Width varies per line for visual interest
      skeleton.style.width = `${70 + Math.random() * 30}%`;
      container.appendChild(skeleton);
    }
  }
}

// Export singleton instance
export const uiEnhancements = new UIEnhancements();
