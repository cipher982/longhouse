/**
 * UI Enhancement Module - Modern visual feedback and animations
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
    this.toastContainer.style.cssText = `
      position: fixed;
      top: 20px;
      right: 20px;
      z-index: 9999;
      display: flex;
      flex-direction: column;
      gap: 12px;
    `;
    document.body.appendChild(this.toastContainer);
  }

  /**
   * Show toast notification
   */
  showToast(message: string, type: 'success' | 'error' | 'info' = 'info'): void {
    if (!this.toastContainer) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;

    const colors = {
      success: 'linear-gradient(135deg, #0ba360 0%, #3cba92 100%)',
      error: 'linear-gradient(135deg, #f43f5e 0%, #e11d48 100%)',
      info: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)'
    };

    const icons = {
      success: '✓',
      error: '✕',
      info: 'ⓘ'
    };

    toast.style.cssText = `
      padding: 16px 20px;
      background: ${colors[type]};
      color: white;
      border-radius: 12px;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.3);
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 14px;
      font-weight: 500;
      animation: slideIn 0.3s ease;
      max-width: 320px;
    `;

    toast.innerHTML = `
      <span style="font-size: 18px;">${icons[type]}</span>
      <span>${message}</span>
    `;

    this.toastContainer.appendChild(toast);

    // Auto-remove after 4 seconds
    setTimeout(() => {
      toast.style.animation = 'slideOut 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  }

  /**
   * Show loading overlay
   */
  showLoading(message = 'Loading...'): HTMLDivElement {
    const overlay = document.createElement('div');
    overlay.className = 'loading-overlay';
    overlay.style.cssText = `
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(10, 14, 39, 0.8);
      backdrop-filter: blur(10px);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      z-index: 9998;
      animation: fadeIn 0.3s ease;
    `;

    overlay.innerHTML = `
      <div class="loading-spinner" style="
        width: 60px;
        height: 60px;
        border: 3px solid rgba(139, 92, 246, 0.2);
        border-top-color: #8b5cf6;
        border-radius: 50%;
        animation: spin 1s linear infinite;
      "></div>
      <div class="loading-text" style="
        margin-top: 20px;
        color: white;
        font-size: 16px;
        font-weight: 500;
      ">${message}</div>
    `;

    document.body.appendChild(overlay);
    return overlay;
  }

  /**
   * Hide loading overlay
   */
  hideLoading(overlay: HTMLDivElement): void {
    overlay.style.animation = 'fadeOut 0.3s ease';
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

    ripple.style.cssText = `
      position: absolute;
      width: ${size}px;
      height: ${size}px;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.5);
      left: ${x}px;
      top: ${y}px;
      animation: rippleEffect 0.6s ease-out;
      pointer-events: none;
    `;

    button.style.position = 'relative';
    button.style.overflow = 'hidden';
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
      skeleton.style.cssText = `
        height: 20px;
        background: linear-gradient(90deg,
          rgba(255, 255, 255, 0.05) 25%,
          rgba(255, 255, 255, 0.1) 50%,
          rgba(255, 255, 255, 0.05) 75%
        );
        background-size: 200% 100%;
        animation: shimmer 1.5s infinite;
        border-radius: 4px;
        margin: 8px 0;
        width: ${70 + Math.random() * 30}%;
      `;
      container.appendChild(skeleton);
    }
  }

}

// Add required CSS animations
const style = document.createElement('style');
style.textContent = `
  @keyframes slideIn {
    from {
      transform: translateX(100%);
      opacity: 0;
    }
    to {
      transform: translateX(0);
      opacity: 1;
    }
  }

  @keyframes slideOut {
    from {
      transform: translateX(0);
      opacity: 1;
    }
    to {
      transform: translateX(100%);
      opacity: 0;
    }
  }

  @keyframes fadeIn {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  @keyframes fadeOut {
    from { opacity: 1; }
    to { opacity: 0; }
  }

  @keyframes spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  @keyframes rippleEffect {
    from {
      transform: scale(0);
      opacity: 1;
    }
    to {
      transform: scale(4);
      opacity: 0;
    }
  }

  @keyframes shimmer {
    0% { background-position: -200% 0; }
    100% { background-position: 200% 0; }
  }
`;
document.head.appendChild(style);

// Export singleton instance
export const uiEnhancements = new UIEnhancements();
