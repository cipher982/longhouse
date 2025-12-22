import React, { useEffect, useRef } from 'react';

// Performance monitoring hook
export function usePerformanceMonitoring(componentName: string) {
  const renderStartTime = useRef<number>(performance.now());
  const mountTimeRef = useRef<number | null>(null);

  useEffect(() => {
    // Capture mount time
    mountTimeRef.current = performance.now();
    const renderTime = mountTimeRef.current - renderStartTime.current;

    // Log performance metrics in development
    if (import.meta.env.MODE === 'development') {
      console.log(`⚡ ${componentName} rendered in ${renderTime.toFixed(2)}ms`);
    }

    // In production, you could send to analytics service
    if (import.meta.env.MODE === 'production' && renderTime > 100) {
      console.warn(`Slow render detected: ${componentName} took ${renderTime.toFixed(2)}ms`);
    }

    return () => {
      if (mountTimeRef.current) {
        const unmountTime = performance.now();
        const lifetime = unmountTime - mountTimeRef.current;

        if (import.meta.env.MODE === 'development') {
          console.log(`⏱️  ${componentName} lifetime: ${lifetime.toFixed(2)}ms`);
        }
      }
    };
  }, [componentName]);

  // Performance measurement utilities
  const measureAsync = async <T,>(
    operation: () => Promise<T>,
    operationName: string
  ): Promise<T> => {
    const start = performance.now();
    try {
      const result = await operation();
      const duration = performance.now() - start;

      if (import.meta.env.MODE === 'development') {
        console.log(`🚀 ${operationName} completed in ${duration.toFixed(2)}ms`);
      }

      return result;
    } catch (error) {
      const duration = performance.now() - start;
      console.error(`❌ ${operationName} failed after ${duration.toFixed(2)}ms:`, error);
      throw error;
    }
  };

  return {
    measureAsync,
    renderTime: mountTimeRef.current ? mountTimeRef.current - renderStartTime.current : 0,
  };
}

// Simple memoization helper for React components
export const memoize = React.memo;

// Debounce hook for performance optimization
export function useDebounce<T>(value: T, delay: number): T {
  const [debouncedValue, setDebouncedValue] = React.useState<T>(value);

  React.useEffect(() => {
    const handler = setTimeout(() => {
      setDebouncedValue(value);
    }, delay);

    return () => {
      clearTimeout(handler);
    };
  }, [value, delay]);

  return debouncedValue;
}

// Memory usage monitoring (development only)
export function useMemoryMonitoring(componentName: string) {
  useEffect(() => {
    if (import.meta.env.MODE !== 'development') return;

    // Check if performance.memory is available
    const perfMemory = (performance as unknown as {
      memory?: {
        usedJSHeapSize: number;
        totalJSHeapSize: number;
        jsHeapSizeLimit: number;
      }
    }).memory;
    if (!perfMemory) return;

    const logMemoryUsage = () => {
      const usedJSHeapSize = Math.round(perfMemory.usedJSHeapSize / 1024 / 1024);
      const totalJSHeapSize = Math.round(perfMemory.totalJSHeapSize / 1024 / 1024);
      const jsHeapSizeLimit = Math.round(perfMemory.jsHeapSizeLimit / 1024 / 1024);

      console.log(`🧠 Memory usage for ${componentName}:`, {
        used: `${usedJSHeapSize}MB`,
        total: `${totalJSHeapSize}MB`,
        limit: `${jsHeapSizeLimit}MB`,
        percentage: `${((usedJSHeapSize / jsHeapSizeLimit) * 100).toFixed(1)}%`,
      });
    };

    // Log initial memory usage
    logMemoryUsage();

    // Set up periodic monitoring
    const interval = setInterval(logMemoryUsage, 10000); // Every 10 seconds

    return () => clearInterval(interval);
  }, [componentName]);
}

// Bundle size warning (development only)
export function useBundleSizeWarning() {
  useEffect(() => {
    if (import.meta.env.MODE !== 'development') return;

    // Check bundle size through performance entries
    const resources = performance.getEntriesByType('resource') as PerformanceResourceTiming[];
    const jsBundle = resources.find(r => r.name.includes('assets/index-') && r.name.endsWith('.js'));

    if (jsBundle && jsBundle.transferSize) {
      const sizeMB = jsBundle.transferSize / (1024 * 1024);

      console.log(`📦 Main bundle size: ${sizeMB.toFixed(2)}MB`);

      if (sizeMB > 1) {
        console.warn(`⚠️  Bundle size is large (${sizeMB.toFixed(2)}MB). Consider code splitting.`);
      }
    }
  }, []);
}

export default usePerformanceMonitoring;
