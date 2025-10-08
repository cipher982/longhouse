/**
 * Shared types for voice agent contexts
 */

export interface VoiceAgentConfig {
  // Core identity
  name: string;
  description: string;
  instructions: string;
  
  // UI/Branding
  theme: {
    primaryColor: string;
    secondaryColor: string;
    backgroundColor: string;
    textColor: string;
    accentColor: string;
    borderColor: string;
  };
  
  branding: {
    title: string;
    subtitle?: string;
    logoUrl?: string;
    favicon?: string;
  };
  
  // Tools and capabilities
  tools: ToolConfig[];
  
  // API endpoints and configuration
  apiEndpoints: {
    tokenMinting: string;
    toolExecution: string;
  };

  // Synchronization / persistence settings
  sync?: {
    baseUrl?: string;
    headers?: Record<string, string>;
  };
  
  // Context-specific settings
  settings: {
    maxHistoryTurns: number;
    enableRAG: boolean;
    enableMCP: boolean;
    voiceModel: string;
    defaultPrompts: string[];
  };
}

export interface ToolConfig {
  name: string;
  description: string;
  enabled: boolean;
  config?: Record<string, any>;
  // For MCP tools
  mcpServer?: string;
  mcpFunction?: string;
  // For RAG tools  
  ragDatabase?: string;
  ragCollection?: string;
}

export interface ContextManifest {
  version: string;
  name: string;
  description: string;
  configFile: string;
  themeFile?: string;
  toolsDirectory?: string;
  requiredEnvVars?: string[];
}
