import { execFile, execFileSync } from "child_process";
import fs from "fs";
import http from "http";
import type { AddressInfo } from "net";
import os from "os";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REPO_ROOT = path.resolve(__dirname, "../..");

let cachedEngineBin: string | null = null;

function resolveCargoProfile(): string {
  const configured = process.env.CARGO_PROFILE?.trim();
  if (configured) return configured;
  return fs.existsSync("/work/.build/cargo-target/ci-test")
    ? "ci-test"
    : "debug";
}

function resolveArtifactPath(profile: string): string {
  return execFileSync(
    "python3",
    [
      path.join(REPO_ROOT, "scripts/build/cargo.py"),
      "artifact",
      "--profile",
      profile,
      "--bin",
      "longhouse-engine",
    ],
    { cwd: REPO_ROOT, encoding: "utf8" },
  ).trim();
}

export function ensureEngineBinary(): string {
  if (cachedEngineBin && fs.existsSync(cachedEngineBin)) {
    return cachedEngineBin;
  }
  const explicit = process.env.LONGHOUSE_ENGINE_BIN?.trim();
  if (explicit) {
    if (!fs.existsSync(explicit)) {
      throw new Error(`LONGHOUSE_ENGINE_BIN does not exist: ${explicit}`);
    }
    cachedEngineBin = explicit;
    return explicit;
  }

  const profile = resolveCargoProfile();
  const binaryPath = resolveArtifactPath(profile);
  // Compile on demand when this E2E lane has not prebuilt the binary yet so
  // both `make test-e2e-core` and `make test-e2e-single` exercise the real binary.
  if (!fs.existsSync(binaryPath)) {
    execFileSync(
      "python3",
      [path.join(REPO_ROOT, "scripts/build/generate_build_identity.py")],
      { cwd: REPO_ROOT, stdio: "inherit" },
    );
    execFileSync(
      "python3",
      [
        path.join(REPO_ROOT, "scripts/build/cargo.py"),
        "exec",
        "--",
        "build",
        "--manifest-path",
        "engine/Cargo.toml",
        "--profile",
        profile,
        "--bin",
        "longhouse-engine",
      ],
      { cwd: REPO_ROOT, stdio: "inherit" },
    );
  }
  if (!fs.existsSync(binaryPath)) {
    throw new Error(`Expected built longhouse-engine at ${binaryPath}`);
  }
  cachedEngineBin = binaryPath;
  return binaryPath;
}

export async function shipTranscriptFile(options: {
  engineBin: string;
  filePath: string;
  backendUrl: string;
  token: string;
  provider: "claude" | "codex";
  machineName: string;
  engineDbPath: string;
}): Promise<Record<string, unknown>> {
  const stdout = await new Promise<string>((resolve, reject) => {
    execFile(
      options.engineBin,
      [
        "ship",
        "--file",
        options.filePath,
        "--url",
        options.backendUrl,
        "--token",
        options.token,
        "--provider",
        options.provider,
        "--db",
        options.engineDbPath,
        "--machine-name",
        options.machineName,
        "--json",
      ],
      {
        cwd: REPO_ROOT,
        encoding: "utf8",
        timeout: 30_000,
      },
      (err, out, stderr) => {
        if (err) {
          reject(
            new Error(
              `longhouse-engine ship failed: ${err.message}\nstdout: ${out}\nstderr: ${stderr}`,
            ),
          );
          return;
        }
        resolve(out);
      },
    );
  });
  const jsonStart = stdout.indexOf("{");
  if (jsonStart < 0) {
    throw new Error(`longhouse-engine ship emitted no JSON summary: ${stdout}`);
  }
  const summary = JSON.parse(stdout.slice(jsonStart)) as Record<string, unknown>;
  if (summary.status !== "ok") {
    throw new Error(`longhouse-engine ship failed: ${JSON.stringify(summary)}`);
  }
  return summary;
}

export function makeTempEngineDir(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

/**
 * Forward requests from the standalone Rust CLI through a loopback proxy that
 * tags each request with X-Test-Worker so ingested rows land in this worker's DB.
 */
export async function withWorkerHeaderProxy<T>(
  backendUrl: string,
  workerId: string,
  fn: (proxyUrl: string) => Promise<T> | T,
): Promise<T> {
  const upstream = new URL(backendUrl);
  const server = http.createServer((req, res) => {
    const headers: http.OutgoingHttpHeaders = {
      ...req.headers,
      "x-test-worker": workerId,
      host: upstream.host,
    };
    const proxyReq = http.request(
      {
        hostname: upstream.hostname,
        port: upstream.port,
        path: req.url,
        method: req.method,
        headers,
      },
      (proxyRes) => {
        res.writeHead(proxyRes.statusCode ?? 502, proxyRes.headers);
        proxyRes.pipe(res);
      },
    );
    proxyReq.on("error", (err) => {
      if (!res.headersSent) {
        res.writeHead(502, { "content-type": "text/plain" });
      }
      res.end(String(err));
    });
    req.pipe(proxyReq);
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  try {
    const addr = server.address() as AddressInfo;
    return await fn(`http://127.0.0.1:${addr.port}`);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}
