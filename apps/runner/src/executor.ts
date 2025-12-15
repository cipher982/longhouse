/**
 * Command executor with subprocess management.
 *
 * Handles running shell commands with:
 * - Real-time stdout/stderr streaming
 * - Timeout enforcement
 * - Output size capping with truncation
 */

import { spawn } from 'child_process';
import type { ExecChunkMessage, ExecDoneMessage, ExecErrorMessage } from './protocol';

const MAX_OUTPUT_SIZE = 50 * 1024; // 50KB
const TRUNCATION_MESSAGE = '\n... [output truncated due to size limit] ...\n';

export interface ExecutionResult {
  exitCode: number;
  durationMs: number;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}

export interface ExecutionCallbacks {
  onStdout: (chunk: string) => void;
  onStderr: (chunk: string) => void;
  onComplete: (exitCode: number, durationMs: number) => void;
  onError: (error: string) => void;
}

export class CommandExecutor {
  private runningJobs: Map<string, any> = new Map();

  /**
   * Execute a command with streaming output.
   *
   * @param jobId - Unique job identifier
   * @param command - Shell command to execute
   * @param timeoutSecs - Timeout in seconds (0 = no timeout)
   * @param callbacks - Callbacks for streaming events
   */
  async execute(
    jobId: string,
    command: string,
    timeoutSecs: number,
    callbacks: ExecutionCallbacks
  ): Promise<void> {
    const startTime = Date.now();
    let totalOutputSize = 0;
    let outputTruncated = false;
    let timedOut = false;

    console.log(`[executor] Starting job ${jobId}: ${command}`);

    // Spawn process using shell
    const child = spawn(command, [], {
      shell: true,
      stdio: ['ignore', 'pipe', 'pipe'], // stdin ignored, stdout/stderr piped
    });

    // Store for cancellation support
    this.runningJobs.set(jobId, child);

    // Set up timeout if specified
    let timeoutHandle: Timer | null = null;
    if (timeoutSecs > 0) {
      timeoutHandle = setTimeout(() => {
        console.log(`[executor] Job ${jobId} timed out after ${timeoutSecs}s`);
        timedOut = true;
        child.kill('SIGTERM');

        // Force kill after 1 second if still running
        setTimeout(() => {
          if (child.exitCode === null) {
            console.log(`[executor] Force killing job ${jobId}`);
            child.kill('SIGKILL');
          }
        }, 1000);
      }, timeoutSecs * 1000);
    }

    // Stream stdout
    child.stdout?.on('data', (data: Buffer) => {
      const chunk = data.toString();
      totalOutputSize += chunk.length;

      if (totalOutputSize > MAX_OUTPUT_SIZE && !outputTruncated) {
        callbacks.onStdout(TRUNCATION_MESSAGE);
        outputTruncated = true;
        child.stdout?.removeAllListeners('data');
        child.stderr?.removeAllListeners('data');
      } else if (!outputTruncated) {
        callbacks.onStdout(chunk);
      }
    });

    // Stream stderr
    child.stderr?.on('data', (data: Buffer) => {
      const chunk = data.toString();
      totalOutputSize += chunk.length;

      if (totalOutputSize > MAX_OUTPUT_SIZE && !outputTruncated) {
        callbacks.onStderr(TRUNCATION_MESSAGE);
        outputTruncated = true;
        child.stdout?.removeAllListeners('data');
        child.stderr?.removeAllListeners('data');
      } else if (!outputTruncated) {
        callbacks.onStderr(chunk);
      }
    });

    // Handle process exit
    child.on('exit', (code, signal) => {
      if (timeoutHandle) {
        clearTimeout(timeoutHandle);
      }

      this.runningJobs.delete(jobId);

      const durationMs = Date.now() - startTime;
      const exitCode = code ?? (signal ? 128 + (signal === 'SIGTERM' ? 15 : 9) : 1);

      if (timedOut) {
        callbacks.onError(`Command timed out after ${timeoutSecs} seconds`);
      } else {
        callbacks.onComplete(exitCode, durationMs);
      }

      console.log(
        `[executor] Job ${jobId} completed: exit_code=${exitCode}, duration=${durationMs}ms, timed_out=${timedOut}`
      );
    });

    // Handle process errors
    child.on('error', (err) => {
      if (timeoutHandle) {
        clearTimeout(timeoutHandle);
      }

      this.runningJobs.delete(jobId);

      const durationMs = Date.now() - startTime;
      console.error(`[executor] Job ${jobId} error:`, err);
      callbacks.onError(err.message);
    });
  }

  /**
   * Cancel a running job.
   *
   * @param jobId - Job to cancel
   * @returns true if job was found and killed, false otherwise
   */
  cancel(jobId: string): boolean {
    const child = this.runningJobs.get(jobId);
    if (!child) {
      return false;
    }

    console.log(`[executor] Canceling job ${jobId}`);
    child.kill('SIGTERM');
    return true;
  }

  /**
   * Get count of running jobs.
   */
  getRunningJobCount(): number {
    return this.runningJobs.size;
  }
}
