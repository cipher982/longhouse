/**
 * UI Controller Module
 * Manages all DOM updates and visual state changes
 */

import { VoiceButtonState, CONFIG } from './config';
import { stateManager } from './state-manager';

/**
 * UI Controller class
 */
export class UIController {
  // DOM element references
  private voiceButton: HTMLButtonElement | null = null;
  private connectButton: HTMLButtonElement | null = null;
  private statusLabel: HTMLDivElement | null = null;
  private voiceButtonContainer: HTMLDivElement | null = null;
  private textInput: HTMLInputElement | null = null;
  private sendButton: HTMLButtonElement | null = null;
  private handsFreeToggle: HTMLInputElement | null = null;
  private sidebar: HTMLDivElement | null = null;
  private sidebarToggle: HTMLButtonElement | null = null;

  // Status label timeout
  private statusTimeout?: NodeJS.Timeout;

  /**
   * Initialize UI controller with DOM elements
   */
  initialize(): void {
    this.cacheElements();
    this.setupEventListeners();
    this.updateButtonState(VoiceButtonState.IDLE);
  }

  /**
   * Cache DOM element references (using actual HTML IDs)
   */
  private cacheElements(): void {
    this.voiceButton = document.getElementById('pttBtn') as HTMLButtonElement;
    this.connectButton = null; // No connect button in current HTML
    this.statusLabel = document.querySelector('.voice-status-label') as HTMLDivElement;
    this.voiceButtonContainer = document.getElementById('voiceButtonContainer') as HTMLDivElement;
    this.textInput = document.getElementById('textInput') as HTMLInputElement;
    this.sendButton = document.getElementById('sendTextBtn') as HTMLButtonElement;
    this.handsFreeToggle = document.getElementById('handsFreeToggle') as HTMLInputElement;
    this.sidebar = document.getElementById('sidebar') as HTMLDivElement;
    this.sidebarToggle = document.getElementById('sidebarToggle') as HTMLButtonElement;
  }

  /**
   * Setup UI event listeners
   */
  private setupEventListeners(): void {
    // Sidebar toggle
    this.sidebarToggle?.addEventListener('click', () => this.toggleSidebar());

    // State manager listeners removed - AppController calls updateButtonState directly
  }

  /**
   * Update voice button state (adds classes to container for CSS compatibility)
   */
  updateButtonState(state: VoiceButtonState): void {
    if (!this.voiceButton || !this.voiceButtonContainer) return;

    // Remove all state classes from container
    this.voiceButtonContainer.classList.remove(
      'state-idle',
      'state-connecting',
      'state-ready',
      'state-speaking',
      'state-responding',
      'state-active',
      'state-processing'
    );

    // Map state to CSS class format
    const stateClassMap = {
      [VoiceButtonState.IDLE]: 'state-ready',        // Ready to connect
      [VoiceButtonState.CONNECTING]: 'state-processing', // Connecting/Processing
      [VoiceButtonState.READY]: 'state-ready',      // Ready to interact
      [VoiceButtonState.SPEAKING]: 'state-active',  // User is speaking
      [VoiceButtonState.RESPONDING]: 'state-active', // Assistant is responding
      [VoiceButtonState.ACTIVE]: 'state-active',
      [VoiceButtonState.PROCESSING]: 'state-processing'
    } as Record<VoiceButtonState, string>;

    // Add new state class to container
    const cssStateClass = stateClassMap[state] || 'state-ready';
    this.voiceButtonContainer.classList.add(cssStateClass);

    // Update ARIA attributes
    switch (state) {
      case VoiceButtonState.IDLE:
        this.voiceButton.disabled = false;
        this.voiceButton.setAttribute('aria-label', 'Disconnected - Click to connect');
        this.voiceButton.setAttribute('data-state', 'idle');
        break;

      case VoiceButtonState.CONNECTING:
        this.voiceButton.disabled = true;
        this.voiceButton.setAttribute('aria-label', 'Connecting...');
        this.voiceButton.setAttribute('data-state', 'connecting');
        break;

      case VoiceButtonState.READY:
        this.voiceButton.disabled = false;
        this.voiceButton.setAttribute('aria-label', 'Ready - Click or hold to interact');
        this.voiceButton.setAttribute('data-state', 'ready');
        break;

      case VoiceButtonState.SPEAKING:
        this.voiceButton.disabled = false;
        this.voiceButton.setAttribute('aria-label', 'Speaking - Release to send');
        this.voiceButton.setAttribute('data-state', 'speaking');
        break;

      case VoiceButtonState.RESPONDING:
        this.voiceButton.disabled = true;
        this.voiceButton.setAttribute('aria-label', 'Assistant is responding...');
        this.voiceButton.setAttribute('data-state', 'responding');
        break;
    }
  }

  /**
   * Update status label
   */
  updateStatus(message: string, temporary = true): void {
    if (!this.statusLabel) return;

    // Clear any existing timeout
    if (this.statusTimeout) {
      clearTimeout(this.statusTimeout);
      this.statusTimeout = undefined;
    }

    // Update content
    this.statusLabel.textContent = message;
    this.statusLabel.setAttribute('aria-live', 'polite');

    // Make visible
    this.statusLabel.style.opacity = '1';

    // Set temporary status timeout if needed
    if (temporary) {
      this.statusTimeout = setTimeout(() => {
        this.clearStatus();
      }, CONFIG.UI.STATUS_LABEL_TIMEOUT_MS);
    }

    stateManager.setStatusActive(true);
  }

  /**
   * Clear status label
   */
  clearStatus(): void {
    if (!this.statusLabel) return;

    this.statusLabel.style.opacity = '0';
    stateManager.setStatusActive(false);

    if (this.statusTimeout) {
      clearTimeout(this.statusTimeout);
      this.statusTimeout = undefined;
    }
  }

  /**
   * Toggle sidebar
   */
  toggleSidebar(): void {
    if (!this.sidebar) return;

    const isOpen = this.sidebar.classList.contains('open');
    if (isOpen) {
      this.sidebar.classList.remove('open');
      this.sidebarToggle?.setAttribute('aria-expanded', 'false');
    } else {
      this.sidebar.classList.add('open');
      this.sidebarToggle?.setAttribute('aria-expanded', 'true');
    }
  }

  /**
   * Show error message
   */
  showError(message: string): void {
    this.updateStatus(`Error: ${message}`, false);
    this.voiceButtonContainer?.classList.add('error');

    setTimeout(() => {
      this.voiceButtonContainer?.classList.remove('error');
    }, CONFIG.UI.ANIMATION_DURATION_MS * 2);
  }

  /**
   * Clear text input
   */
  clearTextInput(): void {
    if (this.textInput) {
      this.textInput.value = '';
    }
  }

  /**
   * Get voice button element
   */
  getVoiceButton(): HTMLButtonElement | null {
    return this.voiceButton;
  }

  /**
   * Get connect button element
   */
  getConnectButton(): HTMLButtonElement | null {
    return this.connectButton;
  }

  /**
   * Get hands-free toggle element
   */
  getHandsFreeToggle(): HTMLInputElement | null {
    return this.handsFreeToggle;
  }

  /**
   * Get text input element
   */
  getTextInput(): HTMLInputElement | null {
    return this.textInput;
  }

  /**
   * Get send button element
   */
  getSendButton(): HTMLButtonElement | null {
    return this.sendButton;
  }

  /**
   * Cleanup
   */
  cleanup(): void {
    if (this.statusTimeout) {
      clearTimeout(this.statusTimeout);
      this.statusTimeout = undefined;
    }
  }
}

// Export singleton instance
export const uiController = new UIController();
