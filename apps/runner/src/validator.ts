/**
 * Command validator for runner-side capability enforcement.
 *
 * Mirrors server-side validation logic to provide defense-in-depth.
 * Validates commands against runner capabilities before execution.
 */

export interface ValidationResult {
  allowed: boolean;
  reason?: string;
}

export class CommandValidator {
  // Shell metacharacters that indicate complex/dangerous commands
  private readonly FORBIDDEN_CHARS = new Set([
    ';',
    '|',
    '&',
    '>',
    '<',
    '$',
    '(',
    ')',
    '`',
    '\n',
    '\\',
  ]);

  // Allowlist for exec.readonly (argv[0] patterns)
  private readonly READONLY_ALLOWLIST = new Set([
    // System read-only commands
    'uname',
    'uptime',
    'date',
    'whoami',
    'id',
    'df',
    'du',
    'free',
    'ps',
    'top',
    'hostname',
    'cat',
    'head',
    'tail',
    'ls',
    'pwd',
    'env',
    'printenv',
    'echo',  // safe read-only command for testing/debugging
    'false', // safe command (exits with 1)
    'true',  // safe command (exits with 0)
    // Commands requiring subcommand validation
    'systemctl',
    'journalctl',
    'docker',
  ]);

  // Docker read-only subcommands
  private readonly DOCKER_READONLY = new Set([
    'ps',
    'logs',
    'stats',
    'inspect',
    'images',
    'info',
    'version',
  ]);

  // Explicitly denied commands (blocklist)
  private readonly DESTRUCTIVE_COMMANDS = new Set([
    'rm',
    'rmdir',
    'mkfs',
    'dd',
    'shutdown',
    'reboot',
    'halt',
    'poweroff',
    'useradd',
    'userdel',
    'usermod',
    'groupadd',
    'passwd',
    'chmod',
    'chown',
    'chgrp',
    'iptables',
    'ip6tables',
    'ufw',
    'firewall-cmd',
    'mount',
    'umount',
    'fdisk',
    'parted',
    'kill',
    'killall',
    'pkill',
  ]);

  /**
   * Validate command against capabilities.
   *
   * @param command - Shell command to validate
   * @param capabilities - List of runner capabilities
   * @returns Validation result with allowed flag and optional reason
   */
  validate(command: string, capabilities: string[]): ValidationResult {
    // exec.full allows everything
    if (capabilities.includes('exec.full')) {
      console.log('[validator] Command allowed via exec.full capability');
      return { allowed: true };
    }

    // exec.readonly requires strict validation
    return this.validateReadonly(command, capabilities);
  }

  /**
   * Check for forbidden shell metacharacters.
   */
  private hasShellMetacharacters(command: string): boolean {
    for (const char of this.FORBIDDEN_CHARS) {
      if (command.includes(char)) {
        return true;
      }
    }
    return false;
  }

  /**
   * Extract the base command (argv[0]).
   */
  private parseArgv0(command: string): string {
    const tokens = command.trim().split(/\s+/);
    if (tokens.length === 0) {
      return '';
    }

    // Handle absolute paths (e.g., /usr/bin/docker -> docker)
    let baseCmd = tokens[0];
    if (baseCmd.includes('/')) {
      baseCmd = baseCmd.split('/').pop() || '';
    }

    return baseCmd;
  }

  /**
   * Validate against exec.readonly allowlist.
   */
  private validateReadonly(command: string, capabilities: string[]): ValidationResult {
    // Check for shell metacharacters
    if (this.hasShellMetacharacters(command)) {
      return {
        allowed: false,
        reason:
          'Command contains shell metacharacters (pipes, redirects, etc). ' +
          'These are not allowed in exec.readonly mode.',
      };
    }

    // Extract base command
    const argv0 = this.parseArgv0(command);
    if (!argv0) {
      return { allowed: false, reason: 'Empty command' };
    }

    // Check destructive commands blocklist
    if (this.DESTRUCTIVE_COMMANDS.has(argv0)) {
      return {
        allowed: false,
        reason: `Command '${argv0}' is explicitly blocked (destructive operation)`,
      };
    }

    // Check allowlist
    if (!this.READONLY_ALLOWLIST.has(argv0)) {
      return {
        allowed: false,
        reason:
          `Command '${argv0}' is not in the readonly allowlist. ` +
          'Grant exec.full capability to run arbitrary commands.',
      };
    }

    // Special validation for specific commands
    if (argv0 === 'systemctl') {
      if (!this.validateSystemctl(command)) {
        return {
          allowed: false,
          reason: "systemctl is only allowed with 'status' subcommand in readonly mode",
        };
      }
    } else if (argv0 === 'journalctl') {
      if (!this.validateJournalctl(command)) {
        return {
          allowed: false,
          reason:
            'journalctl must include --no-pager flag in readonly mode (prevents hanging)',
        };
      }
    } else if (argv0 === 'docker') {
      // Docker requires explicit capability
      if (!capabilities.includes('docker')) {
        return {
          allowed: false,
          reason:
            "docker command requires 'docker' capability. " +
            'Runner must be started with docker.sock mount and docker capability must be granted.',
        };
      }

      // Validate docker subcommand is read-only
      if (!this.validateDocker(command)) {
        const allowed = Array.from(this.DOCKER_READONLY).sort().join(', ');
        return {
          allowed: false,
          reason: `docker subcommand is not allowed in readonly mode. Allowed: ${allowed}`,
        };
      }
    }

    // Command passed all checks
    return { allowed: true };
  }

  /**
   * Validate systemctl command - only allow 'status' subcommand.
   */
  private validateSystemctl(command: string): boolean {
    const tokens = command.trim().split(/\s+/);
    if (tokens.length < 2) {
      return false;
    }

    // Second token should be 'status'
    return tokens[1] === 'status';
  }

  /**
   * Validate journalctl command - must include --no-pager.
   */
  private validateJournalctl(command: string): boolean {
    // Must include --no-pager flag to prevent hanging
    return command.includes('--no-pager');
  }

  /**
   * Validate docker command - only allow read-only subcommands.
   */
  private validateDocker(command: string): boolean {
    const tokens = command.trim().split(/\s+/);
    if (tokens.length < 2) {
      return false;
    }

    // Extract subcommand (second token)
    const subcommand = tokens[1];

    // Check if subcommand is in readonly list
    return this.DOCKER_READONLY.has(subcommand);
  }
}
