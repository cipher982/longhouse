/**
 * Minimal Reporter for AI Fiche Consumption
 *
 * Progressive disclosure pattern:
 * - stdout: 3-4 lines (pass count, fail list if any)
 * - test-results/summary.json: Full queryable results
 * - test-results/errors.txt: Human-readable error details
 *
 * Design principles:
 * - AI fiches need pass/fail signal, not verbose logs
 * - Details available on-demand via file queries
 * - Machine-readable JSON for programmatic access
 */

import type {
  Reporter,
  FullConfig,
  Suite,
  TestCase,
  TestResult,
  FullResult,
} from '@playwright/test/reporter';
import * as fs from 'fs';
import * as path from 'path';

interface TestSummary {
  file: string;
  line: number;
  title: string;
  fullTitle: string;
  status: string;
  duration: number;
  error?: {
    message: string;
    stack?: string;
  };
  retry: number;
}

interface ResultsSummary {
  status: 'passed' | 'failed' | 'timedout' | 'interrupted';
  startTime: string;
  duration: number;
  durationFormatted: string;
  counts: {
    total: number;
    passed: number;
    failed: number;
    skipped: number;
    flaky: number;
  };
  passed: TestSummary[];
  failed: TestSummary[];
  skipped: TestSummary[];
  flaky: TestSummary[];
}

class MinimalReporter implements Reporter {
  private results: ResultsSummary;
  private outputDir: string;
  private startTime: Date;
  private originalConsoleLog: typeof console.log;
  private originalConsoleWarn: typeof console.warn;
  private suppressedLogs: string[] = [];

  constructor(options: { outputDir?: string; suppressTestLogs?: boolean } = {}) {
    this.outputDir = options.outputDir || 'test-results';
    this.startTime = new Date();
    this.results = {
      status: 'passed',
      startTime: this.startTime.toISOString(),
      duration: 0,
      durationFormatted: '',
      counts: { total: 0, passed: 0, failed: 0, skipped: 0, flaky: 0 },
      passed: [],
      failed: [],
      skipped: [],
      flaky: [],
    };

    // Suppress noisy test console.logs by default
    if (options.suppressTestLogs !== false) {
      this.originalConsoleLog = console.log;
      this.originalConsoleWarn = console.warn;

      // Capture but don't print test logs (still available in full-output.log)
      console.log = (...args: unknown[]) => {
        const msg = args.map(a => String(a)).join(' ');
        // Allow setup/teardown messages through (they're useful progress indicators)
        if (msg.includes('Setting up test') || msg.includes('cleanup') ||
            msg.includes('Pre-creating schemas') || msg.includes('schemas ready')) {
          this.originalConsoleLog.apply(console, args);
        }
        this.suppressedLogs.push(msg);
      };

      console.warn = (...args: unknown[]) => {
        this.suppressedLogs.push(`[WARN] ${args.map(a => String(a)).join(' ')}`);
      };
    }
  }

  onBegin(config: FullConfig, suite: Suite): void {
    this.results.counts.total = suite.allTests().length;
    // Single line start message
    this.originalConsoleLog?.call(console, `Running ${suite.allTests().length} tests...`);
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    const summary: TestSummary = {
      file: path.relative(process.cwd(), test.location.file),
      line: test.location.line,
      title: test.title,
      fullTitle: test.titlePath().join(' > '),
      status: result.status,
      duration: result.duration,
      retry: result.retry,
    };

    if (result.error) {
      summary.error = {
        message: result.error.message || 'Unknown error',
        stack: result.error.stack,
      };
    }

    // Categorize results
    if (result.status === 'skipped') {
      this.results.skipped.push(summary);
      this.results.counts.skipped++;
    } else if (result.status === 'passed') {
      if (result.retry > 0) {
        // Flaky: passed on retry
        this.results.flaky.push(summary);
        this.results.counts.flaky++;
      } else {
        this.results.passed.push(summary);
        this.results.counts.passed++;
      }
    } else {
      // Only count as failed if all retries exhausted
      const isLastRetry = result.retry === (test.retries || 0);
      if (isLastRetry) {
        this.results.failed.push(summary);
        this.results.counts.failed++;
      }
    }
  }

  async onEnd(result: FullResult): Promise<void> {
    // Restore console
    if (this.originalConsoleLog) {
      console.log = this.originalConsoleLog;
      console.warn = this.originalConsoleWarn;
    }

    const duration = result.duration;
    this.results.status = result.status;
    this.results.duration = duration;
    this.results.durationFormatted = this.formatDuration(duration);

    // Ensure output directory exists
    fs.mkdirSync(this.outputDir, { recursive: true });

    // Write detailed results
    await this.writeResults();

    // Print minimal summary to stdout
    this.printSummary();
  }

  private async writeResults(): Promise<void> {
    // 1. Write JSON summary (machine-readable)
    const jsonPath = path.join(this.outputDir, 'summary.json');
    fs.writeFileSync(jsonPath, JSON.stringify(this.results, null, 2));

    // 2. Write human-readable errors
    if (this.results.failed.length > 0) {
      const errorsPath = path.join(this.outputDir, 'errors.txt');
      const errorContent = this.results.failed.map(t => {
        return [
          `${'='.repeat(70)}`,
          `FAILED: ${t.file}:${t.line}`,
          `Test: ${t.fullTitle}`,
          `Duration: ${t.duration}ms | Retry: ${t.retry}`,
          ``,
          `Error: ${t.error?.message || 'Unknown'}`,
          ``,
          t.error?.stack || '',
          ``,
        ].join('\n');
      }).join('\n');
      fs.writeFileSync(errorsPath, errorContent);
    }

    // 3. Write suppressed logs (for debugging if needed)
    if (this.suppressedLogs.length > 0) {
      const logsPath = path.join(this.outputDir, 'full-output.log');
      fs.writeFileSync(logsPath, this.suppressedLogs.join('\n'));
    }
  }

  private printSummary(): void {
    const { counts, durationFormatted, failed, flaky } = this.results;
    const log = this.originalConsoleLog || console.log;

    // Build status line
    const parts: string[] = [];
    if (counts.passed > 0) parts.push(`${counts.passed} passed`);
    if (counts.failed > 0) parts.push(`${counts.failed} failed`);
    if (counts.skipped > 0) parts.push(`${counts.skipped} skipped`);
    if (counts.flaky > 0) parts.push(`${counts.flaky} flaky`);

    const icon = counts.failed > 0 ? '\u2717' : '\u2713'; // ✗ or ✓
    const statusLine = `${icon} E2E: ${parts.join(', ')} (${durationFormatted})`;

    log('');
    log(statusLine);

    // If failures, list them (max 10)
    if (failed.length > 0) {
      log('');
      const toShow = failed.slice(0, 10);
      toShow.forEach(t => {
        log(`  ${t.file}:${t.line} "${t.title}"`);
      });
      if (failed.length > 10) {
        log(`  ... and ${failed.length - 10} more`);
      }
      log('');
      log(`\u2192 Errors: cat ${this.outputDir}/errors.txt`);
      log(`\u2192 Query:  jq '.failed[]' ${this.outputDir}/summary.json`);
    }

    // Note flaky tests if any
    if (flaky.length > 0 && failed.length === 0) {
      log(`  (${flaky.length} flaky tests passed on retry)`);
    }

    log('');
  }

  private formatDuration(ms: number): string {
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    const mins = Math.floor(ms / 60000);
    const secs = Math.floor((ms % 60000) / 1000);
    return `${mins}m ${secs}s`;
  }
}

export default MinimalReporter;
