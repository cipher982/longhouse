import type { Runner } from "../services/api";

export function isRunnerSessionLaunchReady(runner: Runner): boolean {
  return runner.status === "online" && runner.capabilities?.includes("exec.full") === true;
}
