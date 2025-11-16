/**
 * UI Controller Module
 * Manages all DOM updates and visual state changes
 */

import { VoiceButtonState, CONFIG } from './config';
import { stateManager } from './state-manager';
import { logger } from '@jarvis/core';

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
   * Cache DOM element references
   */
  private cacheElements(): void {
    this.voiceButton = document.getElementById('voiceButton') as HTMLButtonElement;
    this.connectButton = document.getElementById('connectButton') as HTMLButtonElement;
    this.statusLabel = document.querySelector('.voice-status-label') as HTMLDivElement;
    this.voiceButtonContainer = document.getElementById('voiceButtonContainer') as HTMLDivElement;
    this.textInput = document.getElementById('textInput') as HTMLInputElement;
    this.sendButton = document.getElementById('sendButton') as HTMLButtonElement;
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

    // Listen to state changes
    stateManager.addListener((event) => {
      if (event.type === 'VOICE_BUTTON_STATE_CHANGED') {
        this.updateButtonState(event.state);
      }
    });
  }

  /**
   * Update voice button state (Simplified to 3 states)
   */
  updateButtonState(state: VoiceButtonState): void {
    if (!this.voiceButton || !this.voiceButtonContainer) return;

    // Remove all state classes
    this.voiceButtonContainer.classList.remove(
      'state-ready',
      'state-active',
      'state-processing'
    );

    // Add current state class
    this.voiceButtonContainer.classList.add(`state-${state}`);

    // Update button attributes
    switch (state) {
      case VoiceButtonState.READY:
        this.voiceButton.disabled = false;
        this.voiceButton.setAttribute('aria-label', 'Ready - Click or hold to interact');
        this.voiceButton.setAttribute('data-state', 'ready');
        break;

      case VoiceButtonState.ACTIVE:
        this.voiceButton.disabled = false;
        this.voiceButton.setAttribute('aria-label', 'Active - Listening or speaking');
        this.voiceButton.setAttribute('data-state', 'active');
        break;

      case VoiceButtonState.PROCESSING:
        this.voiceButton.disabled = true;
        this.voiceButton.setAttribute('aria-label', 'Processing...');
        this.voiceButton.setAttribute('data-state', 'processing');
        break;
    }

    // Update connect button
    this.updateConnectButton(state);

    // Update hands-free toggle
    this.updateHandsFreeToggle(state);
  }

  /**
   * Update connect button state
   */
  private updateConnectButton(state: VoiceButtonState): void {
    if (!this.connectButton) return;

    const isConnected = stateManager.isConnected();

    this.connectButton.textContent = isConnected ? 'Disconnect' : 'Connect';
    this.connectButton.disabled = state === VoiceButtonState.PROCESSING;
    this.connectButton.setAttribute('aria-pressed', isConnected.toString());
  }

  /**
   * Update hands-free toggle state
   */
  private updateHandsFreeToggle(state: VoiceButtonState): void {
    if (!this.handsFreeToggle) return;

    const isConnected = stateManager.isConnected();
    this.handsFreeToggle.disabled = !isConnected;

    // Update parent label style
    const label = this.handsFreeToggle.closest('label');
    if (label) {
      if (isConnected) {
        label.classList.remove('disabled');
      } else {
        label.classList.add('disabled');
      }
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
   * Update text input state
   */
  updateTextInputState(enabled: boolean): void {
    if (!this.textInput || !this.sendButton) return;

    this.textInput.disabled = !enabled;
    this.sendButton.disabled = !enabled;

    if (enabled) {
      this.textInput.placeholder = 'Type a message...';
    } else {
      this.textInput.placeholder = 'Connect to start chatting';
    }
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
   * Get text input value
   */
  getTextInputValue(): string {
    return this.textInput?.value || '';
  }

  /**
   * Focus text input
   */
  focusTextInput(): void {
    this.textInput?.focus();
  }

  /**
   * Show connection error
   */
  showError(message: string): void {
    this.updateStatus(`Error: ${message}`, false);
    this.voiceButtonContainer?.classList.add('error');

    // Remove error class after animation
    setTimeout(() => {
      this.voiceButtonContainer?.classList.remove('error');
    }, CONFIG.UI.ANIMATION_DURATION_MS * 2);
  }

  /**
   * Update conversation mode indicator
   */
  updateConversationMode(mode: 'voice' | 'text'): void {
    // Update any UI elements that indicate current mode
    document.body.setAttribute('data-conversation-mode', mode);

    // Update status if needed
    if (mode === 'text') {
      this.updateStatus('Text mode', true);
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