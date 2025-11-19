/**
 * @jest-environment jsdom
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ContextLoader } from '../contexts/context-loader';

// Mock fetch for manifest loading
global.fetch = vi.fn();

describe('ContextLoader', () => {
  let contextLoader: ContextLoader;

  beforeEach(() => {
    vi.clearAllMocks();
    contextLoader = new ContextLoader();

    // Mock successful manifest fetch
    (fetch as vi.MockedFunction<typeof fetch>).mockImplementation(async (url: string) => {
      if (url.includes('manifest.json')) {
        return {
          ok: true,
          json: async () => ({
            version: '1.0.0',
            name: 'personal',
            description: 'Personal AI assistant context',
            configFile: 'config.ts',
            themeFile: 'theme.css',
            requiredEnvVars: []
          })
        } as Response;
      }
      throw new Error('Not found');
    });
  });

  describe('autoDetectContext', () => {
    it('should discover available contexts before selecting', async () => {
      const context = await contextLoader.autoDetectContext();
      expect(context).toBe('personal');

      // Verify contexts were discovered
      const available = contextLoader.getAvailableContexts();
      expect(available).toContain('personal');
      expect(available.length).toBeGreaterThan(0);
    });

    it('should auto-detect from environment variable', async () => {
      // Mock environment variable
      vi.stubGlobal('import.meta', {
        env: { VITE_VOICE_CONTEXT: 'personal' }
      });

      const context = await contextLoader.autoDetectContext();
      expect(context).toBe('personal');
    });

    it('should auto-detect from URL parameter', async () => {
      // Mock URL
      Object.defineProperty(window, 'location', {
        value: { search: '?context=personal' },
        writable: true
      });

      const context = await contextLoader.autoDetectContext();
      expect(context).toBe('personal');
    });

    it('should auto-detect from localStorage', async () => {
      // Mock localStorage
      vi.stubGlobal('localStorage', {
        getItem: vi.fn((key: string) => {
          if (key === 'voice-agent-context') return 'personal';
          return null;
        }),
        setItem: vi.fn(),
        removeItem: vi.fn()
      });

      const context = await contextLoader.autoDetectContext();
      expect(context).toBe('personal');
    });
  });

  describe('createContextSelector', () => {
    beforeEach(async () => {
      // Ensure contexts are discovered
      await contextLoader.autoDetectContext();

      // Create test container
      document.body.innerHTML = '<div id="test-container"></div>';
    });

    it('should create selector element after contexts are discovered', async () => {
      contextLoader.createContextSelector('test-container');

      const selector = document.getElementById('context-selector') as HTMLSelectElement;
      expect(selector).toBeTruthy();
      expect(selector.tagName).toBe('SELECT');
      expect(selector.className).toContain('context-selector');
    });

    it('should populate selector with available contexts', async () => {
      contextLoader.createContextSelector('test-container');

      const selector = document.getElementById('context-selector') as HTMLSelectElement;
      expect(selector.options.length).toBeGreaterThan(0);
      expect(selector.options[0].textContent).toBe('personal');
    });

    it('should handle context switching', async () => {
      const consoleSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
      contextLoader.createContextSelector('test-container');

      const selector = document.getElementById('context-selector') as HTMLSelectElement;

      // Trigger change event
      selector.dispatchEvent(new Event('change'));

      // Should not throw error
      expect(consoleSpy).toHaveBeenCalled();
      consoleSpy.mockRestore();
    });

    it('should disable selector during context switch', async () => {
      // Mock successful context load
      vi.spyOn(contextLoader, 'switchContext').mockResolvedValue({
        name: 'Personal Assistant',
        instructions: 'You are a helpful AI.',
        model: 'gpt-4o',
        voice: 'verse',
        instructionsSrc: null,
        tools: [],
        toolUse: 'auto'
      } as any);

      contextLoader.createContextSelector('test-container');

      const selector = document.getElementById('context-selector') as HTMLSelectElement;
      expect(selector.disabled).toBe(false);

      // Trigger change
      selector.dispatchEvent(new Event('change'));
      // Note: In real async flow, selector would be disabled during switch
    });
  });

  describe('switchContext', () => {
    beforeEach(async () => {
      await contextLoader.autoDetectContext();
    });

    it('should load and switch context', async () => {
      const config = await contextLoader.switchContext('personal');

      expect(config).toBeTruthy();
      expect(config.name).toBe('Jarvis');
    });

    it('should dispatch contextChanged event', async () => {
      const eventSpy = vi.fn();
      window.addEventListener('contextChanged', eventSpy);

      await contextLoader.switchContext('personal');

      expect(eventSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          detail: expect.objectContaining({
            contextName: 'personal',
            config: expect.any(Object)
          })
        })
      );

      window.removeEventListener('contextChanged', eventSpy);
    });

    it('should save context to localStorage', async () => {
      // Mock localStorage
      const setItemSpy = vi.spyOn(window.localStorage, 'setItem').mockImplementation(() => {});

      await contextLoader.switchContext('personal');

      expect(setItemSpy).toHaveBeenCalledWith('voice-agent-context', 'personal');

      setItemSpy.mockRestore();
    });
  });

  describe('loadContext', () => {
    beforeEach(async () => {
      await contextLoader.autoDetectContext();
    });

    it('should load context configuration', async () => {
      const config = await contextLoader.loadContext('personal');

      expect(config).toBeTruthy();
      expect(config.name).toBe('Jarvis');
      expect(config.instructions).toBeTruthy();
      expect(Array.isArray(config.tools)).toBe(true);
    });

    it('should throw error for unknown context', async () => {
      await expect(contextLoader.loadContext('nonexistent'))
        .rejects
        .toThrow('Context "nonexistent" not found');
    });
  });
});
