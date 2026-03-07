export type RunnerNativeInstallMode = "desktop" | "server";

interface RunnerInstallCommandInput {
  enrollToken: string;
  longhouseUrl: string;
  oneLinerInstallCommand?: string | null;
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/$/, "");
}

export function buildRunnerNativeInstallCommand(
  input: RunnerInstallCommandInput,
  mode: RunnerNativeInstallMode,
): string {
  if (mode === "desktop" && input.oneLinerInstallCommand) {
    return input.oneLinerInstallCommand;
  }

  const installUrl = `${trimTrailingSlash(input.longhouseUrl)}/api/runners/install.sh`;
  const envPrefix = mode === "server"
    ? `ENROLL_TOKEN=${input.enrollToken} RUNNER_INSTALL_MODE=server`
    : `ENROLL_TOKEN=${input.enrollToken}`;

  return `${envPrefix} bash -c 'curl -fsSL ${installUrl} | bash'`;
}

export function describeRunnerNativeInstallMode(mode: RunnerNativeInstallMode): string {
  if (mode === "server") {
    return "Always-on Linux server/VM: installs a system service that survives logout and reboot.";
  }

  return "Personal machine: installs as launchd on macOS or a systemd user service on Linux.";
}
