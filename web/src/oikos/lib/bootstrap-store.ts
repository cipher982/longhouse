/**
 * Minimal bootstrap store for data shared outside React.
 * This keeps fallback prompt lookup separate from the chat streaming pipeline.
 */

export interface ModelInfo {
  id: string;
  display_name: string;
  description: string;
  capabilities?: { reasoning?: boolean; reasoningNone?: boolean };
}

export interface ChatPreferences {
  chat_model: string;
  reasoning_effort: 'none' | 'low' | 'medium' | 'high';
}

export interface BootstrapData {
  prompt: string;
  enabled_tools: Array<{ name: string; description: string }>;
  user_context: {
    display_name?: string;
    role?: string;
    location?: string;
    servers?: Array<{ name: string; purpose: string }>;
  };
  available_models: ModelInfo[];
  preferences: ChatPreferences;
}

class BootstrapStore {
  private bootstrap: BootstrapData | null = null;

  setBootstrap(bootstrap: BootstrapData | null): void {
    this.bootstrap = bootstrap;
  }

  getBootstrap(): BootstrapData | null {
    return this.bootstrap;
  }

  reset(): void {
    this.bootstrap = null;
  }
}

export const bootstrapStore = new BootstrapStore();
