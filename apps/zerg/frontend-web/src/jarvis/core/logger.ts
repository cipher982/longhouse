// Centralized logging configuration for Jarvis PWA
// Reduces console spam while maintaining debugging capabilities

export interface LogConfig {
  transport: boolean;      // Transport event logging (dev only)
  deltas: boolean;        // Character-by-character deltas (never recommended)
  performance: boolean;   // Performance timing logs
  errors: boolean;        // Error logs (always enabled in production)
  context: boolean;       // Context loading logs
  conversation: boolean;  // Conversation state changes
  tools: boolean;         // Tool execution logs
  level: 'minimal' | 'normal' | 'verbose' | 'timeline';
}

class Logger {
  private config: LogConfig;
  private transportBatch: string[] = [];
  private batchTimeout: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    const hasWindow = typeof window !== 'undefined';
    const hostname = hasWindow ? window.location.hostname : 'localhost';
    const search = hasWindow ? window.location.search : '';
    const urlParams = new URLSearchParams(search);
    const logLevel = (urlParams.get('log') as LogConfig['level']) || 'normal';
    const isDev = hostname === 'localhost';

    // Timeline mode: minimal noise + timeline logger active
    const isTimeline = logLevel === 'timeline';

    this.config = {
      transport: isDev && logLevel === 'verbose',
      deltas: false,
      performance: logLevel === 'verbose',
      errors: true,
      context: logLevel === 'verbose',
      conversation: logLevel === 'verbose',
      tools: logLevel === 'verbose',
      level: logLevel
    };

    if (typeof console !== 'undefined' && !isTimeline) {
      console.log('📊 Logger initialized:', this.config);
    }
  }

  // Configure logging dynamically
  configure(overrides: Partial<LogConfig>): void {
    this.config = { ...this.config, ...overrides };
    if (typeof console !== 'undefined') {
      console.log('📊 Logger config updated:', this.config);
    }
  }

  // Transport events - batch them to reduce spam
  transport(eventType: string): void {
    if (!this.config.transport) return;

    this.transportBatch.push(eventType);

    if (this.batchTimeout) clearTimeout(this.batchTimeout);

    this.batchTimeout = setTimeout(() => {
      if (this.transportBatch.length > 0) {
        console.log(`📡 Transport events (${this.transportBatch.length}):`, this.transportBatch.join(' → '));
        this.transportBatch = [];
      }
    }, 100); // Batch events for 100ms
  }

  // Character deltas - completely disabled by default
  delta(text: string): void {
    if (!this.config.deltas) return;
    console.debug('🤖 Delta:', text);
  }

  // Performance logs - summarized
  performance(operation: string, duration: number, details?: unknown): void {
    if (!this.config.performance) return;
    console.log(`⚡ ${operation}: ${duration}ms`, details || '');
  }

  // Error logs - always shown
  error(message: string, error?: unknown): void {
    console.error(`❌ ${message}`, error || '');
  }

  // Warning logs - always shown
  warn(message: string, data?: unknown): void {
    console.warn(`⚠️ ${message}`, data || '');
  }

  // Info logs - context aware
  info(message: string, data?: unknown): void {
    if (this.config.level === 'minimal' || this.config.level === 'timeline') return;
    console.log(`ℹ️ ${message}`, data || '');
  }

  // Context loading logs
  context(message: string, data?: unknown): void {
    if (!this.config.context) return;
    console.log(`🎨 ${message}`, data || '');
  }

  // Conversation state logs - reduced frequency
  conversation(message: string, count?: number): void {
    if (!this.config.conversation) return;

    const suffix = count !== undefined ? ` (${count} items)` : '';
    console.log(`💬 ${message}${suffix}`);
  }

  // Tool execution logs
  tool(toolName: string, message: string, data?: unknown): void {
    if (!this.config.tools) return;
    console.log(`🔧 ${toolName}: ${message}`, data || '');
  }

  // Success/completion logs
  success(message: string, data?: unknown): void {
    console.log(`✅ ${message}`, data || '');
  }

  // Debug logs - only in verbose mode
  debug(message: string, data?: unknown): void {
    if (this.config.level !== 'verbose') return;
    console.debug(`🐛 ${message}`, data || '');
  }

  // Streaming response - aggregate instead of character-by-character
  streamingResponse(text: string, isComplete = false): void {
    if (isComplete) {
      const preview = text.length > 50 ? text.substring(0, 50) + '...' : text;
      console.log(`🤖 Response complete: "${preview}" (${text.length} chars)`);
    }
  }
}

// Export singleton instance
export const logger = new Logger();

if (typeof globalThis !== 'undefined') {
  (globalThis as Record<string, unknown>).jarvisLogger = logger;
}
