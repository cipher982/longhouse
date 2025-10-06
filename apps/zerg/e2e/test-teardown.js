/**
 * Global test teardown - runs once after all tests complete
 * Modern testing practices 2025: Automatic cleanup of test artifacts
 */

import { spawn, execSync } from 'child_process';
import path from 'path';

async function globalTeardown(config) {
  console.log('🧹 Starting test environment cleanup...');
  
  try {
    // Resolve a Python interpreter ('python' or 'python3')
    const pythonCmd = (() => {
      try { execSync('python --version', { stdio: 'ignore' }); return 'python'; } catch {}
      try { execSync('python3 --version', { stdio: 'ignore' }); return 'python3'; } catch {}
      return null;
    })();

    if (!pythonCmd) {
      throw new Error("No Python interpreter found (python/python3)");
    }

    // Call Python cleanup script to remove all test databases
    const cleanup = spawn(pythonCmd, ['-c', `
import sys
sys.path.insert(0, '${path.resolve('../backend')}')
from zerg.test_db_manager import cleanup_test_databases
cleanup_test_databases()
print("✅ Test database cleanup completed")
    `], {
      cwd: path.resolve('../backend'),
      stdio: 'inherit'
    });
    // If this fails, the environment must expose 'python' on PATH.
    
    await new Promise((resolve, reject) => {
      cleanup.on('close', (code) => {
        if (code === 0) {
          resolve();
        } else {
          reject(new Error(`Cleanup failed with code ${code}`));
        }
      });
    });
    
    console.log('✅ Test environment cleanup completed');
    
  } catch (error) {
    console.error('❌ Test cleanup failed:', error.message);
    // Continue anyway - don't fail the entire test run due to cleanup issues
  }
}

export default globalTeardown;
