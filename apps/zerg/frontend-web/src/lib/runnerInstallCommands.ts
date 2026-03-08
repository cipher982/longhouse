export type RunnerNativeInstallMode = "desktop" | "server";

interface RunnerInstallCommandInput {
  enrollToken: string;
  longhouseUrl: string;
  oneLinerInstallCommand?: string | null;
  runnerName?: string | null;
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/$/, "");
}

export function buildRunnerNativeInstallCommand(
  input: RunnerInstallCommandInput,
  mode: RunnerNativeInstallMode,
): string {
  if (mode === "desktop" && input.oneLinerInstallCommand && !input.runnerName) {
    return input.oneLinerInstallCommand;
  }

  const installUrl = `${trimTrailingSlash(input.longhouseUrl)}/api/runners/install.sh`;
  const envParts = [`ENROLL_TOKEN=${input.enrollToken}`];
  if (input.runnerName) {
    envParts.push(`RUNNER_NAME=${input.runnerName}`);
  }
  if (mode === "server") {
    envParts.push("RUNNER_INSTALL_MODE=server");
  }

  return `${envParts.join(" ")} bash -c 'curl -fsSL ${installUrl} | bash'`;
}

export function describeRunnerNativeInstallMode(mode: RunnerNativeInstallMode): string {
  if (mode === "server") {
    return "Always-on Linux server/VM: installs a system service that survives logout and reboot.";
  }

  return "Personal machine: installs as launchd on macOS or a systemd user service on Linux.";
}
