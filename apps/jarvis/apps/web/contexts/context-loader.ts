/**
 * Context loader for voice agent configurations
 * Handles runtime switching between personal and work contexts
 */

import type { VoiceAgentConfig, ContextManifest } from './types';
import { logger } from '@jarvis/core';

export class ContextLoader {
  private currentContext: string | null = null;
  private currentConfig: VoiceAgentConfig | null = null;
  private availableContexts: Map<string, ContextManifest> = new Map();

  constructor() {
    // Note: discoverContexts() is async and called later
  }

  /**
   * Discover available contexts by checking manifest files
   */
  private async discoverContexts(): Promise<void> {
    const knownContexts = ['personal'];

    for (const contextName of knownContexts) {
      try {
        const manifestResponse = await fetch(`./contexts/${contextName}/manifest.json`);
        if (manifestResponse.ok) {
          const manifest: ContextManifest = await manifestResponse.json();
          this.availableContexts.set(contextName, manifest);
          logger.context(`Discovered context: ${contextName}`);
        }
      } catch (error) {
        // In unit tests (jsdom) and some dev setups, static assets may not be served.
        // The config is still bundled, so treat the context as available with a minimal manifest.
        if (contextName === 'personal') {
          this.availableContexts.set(contextName, {
            version: '0',
            name: 'personal',
            description: 'Personal AI assistant context',
            configFile: 'config.ts',
            themeFile: 'theme.css',
            requiredEnvVars: [],
          });
          logger.context(`Discovered context: ${contextName} (fallback manifest)`);
        } else {
          logger.debug(`Context ${contextName} not available`, error);
        }
      }
    }
  }

  /**
   * Get list of available contexts
   */
  getAvailableContexts(): string[] {
    return Array.from(this.availableContexts.keys());
  }

  /**
   * Load a specific context configuration
   */
  async loadContext(contextName: string): Promise<VoiceAgentConfig> {
    logger.context(`Loading context: ${contextName}`);

    const manifest = this.availableContexts.get(contextName);
    if (!manifest) {
      throw new Error(`Context "${contextName}" not found. Available: ${this.getAvailableContexts().join(', ')}`);
    }

    try {
      // Dynamic import of the context config
      let configModule: any;
      if (contextName === 'personal') {
        configModule = await import('./personal/config');
      } else {
        throw new Error(`Unknown context: ${contextName}`);
      }

      const configKey = `${contextName}Config`;

      if (!configModule[configKey]) {
        throw new Error(`Config export "${configKey}" not found in ${contextName} context`);
      }

      const config: VoiceAgentConfig = configModule[configKey];

      // Validate required environment variables
      if (manifest.requiredEnvVars) {
        const missingVars = manifest.requiredEnvVars.filter(varName =>
          !import.meta.env?.[`VITE_${varName}`]
        );

        if (missingVars.length > 0) {
          logger.warn(`Missing environment variables for ${contextName}`, missingVars);
        }
      }

      // Load theme CSS if specified
      if (manifest.themeFile) {
        await this.loadTheme(contextName, manifest.themeFile);
      }

      this.currentContext = contextName;
      this.currentConfig = config;

      logger.success(`Context loaded: ${contextName} (${config.name})`);
      return config;

    } catch (error) {
      logger.error(`Failed to load context ${contextName}`, error);
      throw new Error(`Failed to load context "${contextName}": ${error}`);
    }
  }

  /**
   * Load theme CSS for a context
   */
  private async loadTheme(contextName: string, themeFile: string): Promise<void> {
    try {
      // Remove existing theme
      const existingTheme = document.getElementById('context-theme');
      if (existingTheme) {
        existingTheme.remove();
      }

      // Add new theme
      const link = document.createElement('link');
      link.id = 'context-theme';
      link.rel = 'stylesheet';
      link.href = `./contexts/${contextName}/${themeFile}`;

      document.head.appendChild(link);

      // Update body class for context-specific styling
      document.body.className = document.body.className
        .replace(/\w+-context/g, '')  // Remove existing context classes
        .trim();
      document.body.classList.add(`${contextName}-context`);

      logger.context(`Theme loaded: ${contextName}/${themeFile}`);
    } catch (error) {
      logger.warn(`Failed to load theme for ${contextName}`, error);
    }
  }

  /**
   * Get current context configuration
   */
  getCurrentContext(): { name: string | null; config: VoiceAgentConfig | null } {
    return {
      name: this.currentContext,
      config: this.currentConfig
    };
  }

  /**
   * Auto-detect context from environment or URL
   */
  async autoDetectContext(): Promise<string> {
    // Ensure contexts are discovered first
    await this.discoverContexts();
    // Check environment variable first
    const envContext = import.meta.env?.VITE_VOICE_CONTEXT;
    if (envContext && this.availableContexts.has(envContext)) {
      logger.context(`Context from environment: ${envContext}`);
      return envContext;
    }

    // Check URL parameters
    const urlParams = new URLSearchParams(window.location.search);
    const urlContext = urlParams.get('context');
    if (urlContext && this.availableContexts.has(urlContext)) {
      logger.context(`Context from URL: ${urlContext}`);
      return urlContext;
    }

    // Check localStorage for previous selection
    const savedContext = localStorage.getItem('voice-agent-context');
    if (savedContext && this.availableContexts.has(savedContext)) {
      logger.context(`Context from localStorage: ${savedContext}`);
      return savedContext;
    }

    // Default to personal context
    logger.context('Using default context: personal');
    return 'personal';
  }

  /**
   * Switch context and save preference
   */
  async switchContext(contextName: string): Promise<VoiceAgentConfig> {
    const config = await this.loadContext(contextName);

    // Save preference
    localStorage.setItem('voice-agent-context', contextName);

    // Dispatch event for UI updates
    window.dispatchEvent(new CustomEvent('contextChanged', {
      detail: { contextName, config }
    }));

    return config;
  }

  /**
   * Create context selector UI
   */
  createContextSelector(containerId: string): void {
    const container = document.getElementById(containerId);
    if (!container) {
      logger.error(`Container #${containerId} not found for context selector`);
      return;
    }

    const selector = document.createElement('select');
    selector.id = 'context-selector';
    selector.className = 'context-selector';

    // Add options for available contexts
    for (const [contextName, manifest] of this.availableContexts) {
      const option = document.createElement('option');
      option.value = contextName;
      option.textContent = manifest.name;

      if (contextName === this.currentContext) {
        option.selected = true;
      }

      selector.appendChild(option);
    }

    // Handle context switching
    selector.addEventListener('change', async (event) => {
      const target = event.target as HTMLSelectElement;
      const newContext = target.value;

      if (newContext !== this.currentContext) {
        try {
          selector.disabled = true;
          await this.switchContext(newContext);
          logger.success(`Context: ${newContext}`);
        } catch (error) {
          logger.error('Failed to switch context', error);
          // Revert selection
          target.value = this.currentContext || 'personal';
        } finally {
          selector.disabled = false;
        }
      }
    });

    container.appendChild(selector);
  }
}

// Global context loader instance
export const contextLoader = new ContextLoader();
