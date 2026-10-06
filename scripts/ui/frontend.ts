/**
 * Serve the web app for fixture captures. Shared by ui-capture.ts (one
 * capture) and ui-gallery.ts (one Vite for the whole sweep).
 */
import { spawn } from "child_process";
import path from "path";
import { fileURLToPath } from "url";

export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

export async function isServing(url: string): Promise<boolean> {
  try {
    const response = await fetch(url);
    return response.status < 500;
  } catch {
    return false;
  }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Make sure something serves the web app at baseUrl. If nothing does and the
 * host is local, start Vite in web/ on that port and return a function that
 * stops it. If something already serves it, return a no-op: it is not ours.
 *
 * handleSignals: false when the caller owns Ctrl-C once this returns; Ctrl-C
 * during startup still stops the Vite this started.
 */
export async function ensureFrontend(
  baseUrl: string,
  { handleSignals = true }: { handleSignals?: boolean } = {},
): Promise<() => Promise<void>> {
  if (await isServing(baseUrl)) {
    console.log(`Frontend already serving at ${baseUrl} (not owned by this capture)`);
    return async () => {};
  }
  const target = new URL(baseUrl);
  if (!["localhost", "127.0.0.1", "[::1]"].includes(target.hostname)) {
    throw new Error(`Nothing serves ${baseUrl} and it is not local; start it or change FRONTEND_URL.`);
  }
  const port = target.port || "80";
  console.log(`Nothing listening at ${baseUrl}; starting Vite on :${port} for this capture...`);

  const output: string[] = [];
  const child = spawn("bunx", ["vite", "--port", port, "--strictPort", "--clearScreen", "false"], {
    cwd: path.join(REPO_ROOT, "web"),
    stdio: ["ignore", "pipe", "pipe"],
    detached: true,
  });
  child.stdout?.on("data", (chunk) => output.push(String(chunk)));
  child.stderr?.on("data", (chunk) => output.push(String(chunk)));

  const stop = async () => {
    if (child.exitCode !== null || child.signalCode !== null || child.pid == null) return;
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {
      /* already gone */
    }
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline && child.exitCode === null && child.signalCode === null) {
      await sleep(100);
    }
    if (child.exitCode === null && child.signalCode === null) {
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch {
        /* already gone */
      }
    }
    console.log("Stopped the Vite server this capture started.");
  };

  const onSignal = () => {
    void stop().finally(() => process.exit(130));
  };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);
  const releaseSignals = () => {
    if (handleSignals) return;
    process.off("SIGINT", onSignal);
    process.off("SIGTERM", onSignal);
  };

  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      releaseSignals();
      throw new Error(`Vite exited before serving:\n${output.join("")}`);
    }
    if (await isServing(baseUrl)) {
      console.log(`Vite ready at ${baseUrl}`);
      releaseSignals();
      return stop;
    }
    await sleep(250);
  }
  await stop();
  releaseSignals();
  throw new Error(`Vite did not start serving ${baseUrl} within 60s:\n${output.join("")}`);
}
