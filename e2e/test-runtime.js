import crypto from "crypto";
import fs from "fs";
import path from "path";

const SENSITIVE_ENV =
  /^(?:AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|AZURE_[A-Z0-9_]+|GOOGLE_[A-Z0-9_]+|GCP_[A-Z0-9_]+|OPENAI_API_KEY|ANTHROPIC_API_KEY|CLAUDE_API_KEY|GEMINI_API_KEY|COHERE_API_KEY|MISTRAL_API_KEY|GROQ_API_KEY|XAI_API_KEY|TOGETHER_API_KEY|DEEPSEEK_API_KEY|HF_TOKEN|HUGGINGFACE_TOKEN|DATABASE_URL|REDIS_URL|PG(?:HOST|PORT|USER|PASSWORD|DATABASE)|LONGHOUSE_API_URL|APP_PUBLIC_URL|PUBLIC_SITE_URL|FERNET_SECRET|[A-Z0-9_]+_(?:API_KEY|TOKEN|SECRET))$/;
export function stripAmbientSecrets(environment = process.env) {
  for (const key of Object.keys(environment)) {
    if (SENSITIVE_ENV.test(key)) delete environment[key];
  }
  return environment;
}

function isInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return (
    relative !== "" &&
    relative !== ".." &&
    !relative.startsWith(`..${path.sep}`) &&
    !path.isAbsolute(relative)
  );
}

function requiredChildPath(root, value, fallback, name) {
  const candidate = path.resolve(value || path.join(root, fallback));
  if (!isInside(root, candidate)) {
    throw new Error(`${name} must be inside LONGHOUSE_TEST_ROOT: ${candidate}`);
  }
  return candidate;
}

function ensureAbsoluteRoot(value) {
  if (!value || !path.isAbsolute(value)) {
    throw new Error(
      "LONGHOUSE_TEST_ROOT must be an absolute path when LONGHOUSE_TEST_ISOLATED=1",
    );
  }
  const root = path.resolve(value);
  if (root === path.parse(root).root) {
    throw new Error("LONGHOUSE_TEST_ROOT may not be a filesystem root");
  }
  return root;
}

export function ensureTestRuntime() {
  if (
    process.env.LONGHOUSE_TEST_ISOLATED !== "1" ||
    !fs.existsSync("/tmp/longhouse-test-isolated")
  ) {
    throw new Error(
      "E2E requires the isolated test lane; use make test-e2e or scripts/qa/test-isolation.py --command",
    );
  }
  const root = ensureAbsoluteRoot(process.env.LONGHOUSE_TEST_ROOT);

  const runtime = {
    root,
    dbDir: requiredChildPath(root, process.env.E2E_DB_DIR, "db", "E2E_DB_DIR"),
    artifactDir: requiredChildPath(
      root,
      process.env.E2E_ARTIFACT_DIR,
      "artifacts",
      "E2E_ARTIFACT_DIR",
    ),
    home: requiredChildPath(root, process.env.HOME, "home", "HOME"),
    xdgConfig: requiredChildPath(
      root,
      process.env.XDG_CONFIG_HOME,
      "xdg-config",
      "XDG_CONFIG_HOME",
    ),
    xdgCache: requiredChildPath(
      root,
      process.env.XDG_CACHE_HOME,
      "xdg-cache",
      "XDG_CACHE_HOME",
    ),
    xdgData: requiredChildPath(
      root,
      process.env.XDG_DATA_HOME,
      "xdg-data",
      "XDG_DATA_HOME",
    ),
    xdgState: requiredChildPath(
      root,
      process.env.XDG_STATE_HOME,
      "xdg-state",
      "XDG_STATE_HOME",
    ),
    longhouseHome: requiredChildPath(
      root,
      process.env.LONGHOUSE_HOME,
      "longhouse-home",
      "LONGHOUSE_HOME",
    ),
  };

  for (const directory of Object.values(runtime)) {
    fs.mkdirSync(directory, { recursive: true });
  }

  process.env.LONGHOUSE_TEST_ISOLATED = "1";
  process.env.LONGHOUSE_TEST_ROOT = runtime.root;
  process.env.E2E_DB_DIR = runtime.dbDir;
  process.env.E2E_ARTIFACT_DIR = runtime.artifactDir;
  process.env.HOME = runtime.home;
  process.env.XDG_CONFIG_HOME = runtime.xdgConfig;
  process.env.XDG_CACHE_HOME = runtime.xdgCache;
  process.env.XDG_DATA_HOME = runtime.xdgData;
  process.env.XDG_STATE_HOME = runtime.xdgState;
  process.env.LONGHOUSE_HOME = runtime.longhouseHome;

  return runtime;
}

export function safeChildEnvironment(extra = {}) {
  const environment = { ...process.env, ...extra };
  for (const key of Object.keys(environment)) {
    if (SENSITIVE_ENV.test(key)) delete environment[key];
  }
  environment.LONGHOUSE_TEST_ISOLATED = "1";
  return environment;
}

export function parsePort(value, name) {
  const port = Number.parseInt(value ?? "", 10);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error(`${name} must be a TCP port between 1024 and 65535`);
  }
  return port;
}

export function randomPort() {
  return crypto.randomInt(30_000, 60_000);
}

export function signalProcessGroup(pid, signal) {
  if (process.platform === "win32" || !pid) return false;
  try {
    process.kill(-pid, signal);
    return true;
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
    return false;
  }
}
