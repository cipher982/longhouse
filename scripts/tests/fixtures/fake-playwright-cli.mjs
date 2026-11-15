// Stand-in for playwright-core/cli.js used by playwright-store.test.mjs.
// Modes (FAKE_MODE): ok (default) | hang | fail. FAKE_LOG gets one line per
// real install; FAKE_PIDFILE records the installer pid so tests can prove it
// was killed.
import fs from "node:fs";
import path from "node:path";

const store = process.env.PLAYWRIGHT_BROWSERS_PATH;
const dirs = [path.join(store, "chromium-9999"), path.join(store, "ffmpeg-1")];
const args = process.argv.slice(2);

if (args.includes("--dry-run")) {
  for (const dir of dirs) console.log(`browser: fake\n  Install location:    ${dir}\n`);
  process.exit(0);
}

fs.appendFileSync(process.env.FAKE_LOG, `${process.pid}\n`);
if (process.env.FAKE_PIDFILE) fs.writeFileSync(process.env.FAKE_PIDFILE, String(process.pid));
// A partial download, in the directory the store handed us as TMPDIR.
fs.writeFileSync(path.join(process.env.TMPDIR, "playwright-download-partial.zip"), "x".repeat(1024));
console.log("Downloading fake browser");

const mode = process.env.FAKE_MODE ?? "ok";
if (mode === "hang") setInterval(() => {}, 1000);
else if (mode === "fail") {
  console.error("boom: download failed");
  process.exit(1);
} else {
  setTimeout(() => {
    for (const dir of dirs) {
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, "INSTALLATION_COMPLETE"), "");
    }
    process.exit(0);
  }, Number(process.env.FAKE_DELAY_MS ?? 300));
}
