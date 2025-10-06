// Configuration management for React frontend
// Centralizes environment variables and settings

export interface AppConfig {
  // API Configuration
  apiBaseUrl: string;
  wsBaseUrl: string;

  // Authentication
  googleClientId: string;
  authEnabled: boolean;

  // Environment
  isDevelopment: boolean;
  isProduction: boolean;
  isTesting: boolean;

  // Features
  enablePerformanceMonitoring: boolean;
  enableMemoryMonitoring: boolean;
  enableErrorReporting: boolean;

  // Timeouts and intervals
  wsReconnectInterval: number;
  wsMaxReconnectAttempts: number;
  queryStaleTime: number;
  queryRetryDelay: number;
}

// Extend window type to include runtime config
declare global {
  interface Window {
    API_BASE_URL?: string;
    WS_BASE_URL?: string;
  }
}

// Load configuration from environment variables
function loadConfig(): AppConfig {
  const isDevelopment = import.meta.env.MODE === 'development';
  const isProduction = import.meta.env.MODE === 'production';
  const isTesting = import.meta.env.MODE === 'test';

  // FAIL FAST: No fallbacks, no silent defaults
  // Production MUST have config.js loaded with API_BASE_URL and WS_BASE_URL
  const apiBaseUrl = typeof window !== 'undefined' && window.API_BASE_URL
    ? window.API_BASE_URL
    : (import.meta.env.VITE_API_BASE_URL || (isDevelopment ? '/api' : ''));

  const wsBaseUrl = typeof window !== 'undefined' && window.WS_BASE_URL
    ? window.WS_BASE_URL
    : (import.meta.env.VITE_WS_BASE_URL || (isDevelopment && typeof window !== 'undefined' ? window.location.origin.replace('http', 'ws') : ''));

  // Validate required config in production
  if (isProduction) {
    if (!apiBaseUrl) {
      throw new Error('FATAL: API_BASE_URL not configured! Add window.API_BASE_URL in config.js');
    }
    if (!wsBaseUrl) {
      throw new Error('FATAL: WS_BASE_URL not configured! Add window.WS_BASE_URL in config.js');
    }
  }

  return {
    // API Configuration
    apiBaseUrl,
    wsBaseUrl,

    // Authentication
    googleClientId: import.meta.env.VITE_GOOGLE_CLIENT_ID || "REDACTED_OAUTH_CLIENT.apps.googleusercontent.com",
    authEnabled: import.meta.env.VITE_AUTH_ENABLED !== 'false',

    // Environment
    isDevelopment,
    isProduction,
    isTesting,

    // Features
    enablePerformanceMonitoring: isDevelopment || import.meta.env.VITE_ENABLE_PERFORMANCE === 'true',
    enableMemoryMonitoring: isDevelopment || import.meta.env.VITE_ENABLE_MEMORY_MONITORING === 'true',
    enableErrorReporting: isProduction || import.meta.env.VITE_ENABLE_ERROR_REPORTING === 'true',

    // Timeouts and intervals (in milliseconds)
    wsReconnectInterval: parseInt(import.meta.env.VITE_WS_RECONNECT_INTERVAL || '5000'),
    wsMaxReconnectAttempts: parseInt(import.meta.env.VITE_WS_MAX_RECONNECT_ATTEMPTS || '5'),
    queryStaleTime: parseInt(import.meta.env.VITE_QUERY_STALE_TIME || '300000'), // 5 minutes
    queryRetryDelay: parseInt(import.meta.env.VITE_QUERY_RETRY_DELAY || '1000'),
  };
}

// Global configuration instance
export const config: AppConfig = loadConfig();

// Validation function to ensure required configuration is present
export function validateConfig(): { valid: boolean; errors: string[] } {
  const errors: string[] = [];

  if (!config.googleClientId) {
    errors.push('VITE_GOOGLE_CLIENT_ID is required for authentication');
  }

  if (!config.apiBaseUrl) {
    errors.push('API base URL is required');
  }

  if (config.wsReconnectInterval < 1000) {
    errors.push('WebSocket reconnect interval should be at least 1000ms');
  }

  if (config.wsMaxReconnectAttempts < 1) {
    errors.push('WebSocket max reconnect attempts should be at least 1');
  }

  return {
    valid: errors.length === 0,
    errors,
  };
}

// Environment-specific configuration getters
export const getApiConfig = () => ({
  baseUrl: config.apiBaseUrl,
  timeout: config.isProduction ? 10000 : 30000,
  retries: config.isProduction ? 3 : 1,
});

export const getWebSocketConfig = () => ({
  baseUrl: config.wsBaseUrl,
  reconnectInterval: config.wsReconnectInterval,
  maxReconnectAttempts: config.wsMaxReconnectAttempts,
  includeAuth: config.authEnabled,
});

export const getPerformanceConfig = () => ({
  enableMonitoring: config.enablePerformanceMonitoring,
  enableMemoryMonitoring: config.enableMemoryMonitoring,
  enableBundleSizeWarning: config.isDevelopment,
});

// Development-only configuration validator
if (config.isDevelopment) {
  const validation = validateConfig();
  if (!validation.valid) {
    console.warn('⚠️  Configuration issues detected:', validation.errors);
  } else {
    console.log('✅ Configuration validation passed');
  }

  // Log current configuration in development
  console.log('🔧 App Configuration:', {
    environment: import.meta.env.MODE,
    apiBaseUrl: config.apiBaseUrl,
    authEnabled: config.authEnabled,
    performanceMonitoring: config.enablePerformanceMonitoring,
  });
}

export default config;