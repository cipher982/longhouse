// Page harness for the iOS transcript document: loads the shipped
// ios/Resources/Transcript/transcript.html in Playwright WebKit at phone size
// (the engine WKWebView uses), plays renderer calls the way the app makes
// them, and samples where the reader's anchor row sits on every frame.
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { webkit, type Browser, type BrowserContext, type Page } from "playwright";

const ROOT = path.resolve(import.meta.dirname, "../../..");

export interface Item {
  id: string;
  kind: string;
  [key: string]: unknown;
}

export interface Frame {
  t: number;
  /** Anchor row top, viewport-relative. null when the anchor row is gone. */
  top: number | null;
  y: number;
  height: number;
  viewport: number;
  /** Distance of the scroll offset from the bottom (0 = pinned). */
  gap: number;
  /** Cumulative distance the reader has dragged since sampling began (positive = down). */
  user: number;
}

/** The document with the app's palette spliced in, as the app assembles it. */
export function documentHtml(): string {
  const html = readFileSync(path.join(ROOT, "ios/Resources/Transcript/transcript.html"), "utf8");
  const swift = readFileSync(path.join(ROOT, "ios/Sources/LonghouseApp/Session/Transcript/TranscriptPalette.swift"), "utf8");
  const block = swift
    .slice(swift.indexOf('"""') + 3, swift.lastIndexOf('"""'))
    .replace(/\\\(attentionHexLight\)/g, "#bf3e1a")
    .replace(/\\\(attentionHexDark\)/g, "#e4572e");
  return html.replace("/* __LH_ROOT_BLOCK__ */", block);
}

const SVG = (w: number, h: number, label: string) =>
  `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><rect width="100%" height="100%" fill="#6b8e9e"/><text x="20" y="40" font-size="28" fill="#fff">${label}</text></svg>`;

export const VIEWPORT = { width: 390, height: 640 } as const;

export class Harness {
  browser!: Browser;
  context!: BrowserContext;
  page!: Page;
  seq = 0;
  mediaDelayMs = 350;
  private html = documentHtml();

  static async launch(): Promise<Browser> {
    return webkit.launch();
  }

  async open(browser: Browser, opts: { mediaDelayMs?: number; dark?: boolean } = {}): Promise<void> {
    this.browser = browser;
    this.mediaDelayMs = opts.mediaDelayMs ?? 350;
    this.context = await browser.newContext({
      viewport: { ...VIEWPORT },
      deviceScaleFactor: 2,
      isMobile: true,
      hasTouch: true,
      colorScheme: opts.dark ? "dark" : "light",
    });
    this.page = await this.context.newPage();
    await this.page.route("**/m/*.svg", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, this.mediaDelayMs));
      const id = route.request().url().split("/").pop()!.replace(".svg", "");
      // 600x400 regardless of what the item declared: the real image decides.
      await route.fulfill({ body: SVG(600, 400, id), contentType: "image/svg+xml" });
    });
    await this.page.route("http://probe.local/", (route) => route.fulfill({ body: this.html, contentType: "text/html" }));
    await this.page.goto("http://probe.local/");
    await this.page.evaluate(SAMPLER);
  }

  async close(): Promise<void> {
    await this.context?.close();
  }

  /** `window.renderTranscript` exactly as native calls it. */
  async render(items: Item[], stick: boolean, errorMessage: string | null = null): Promise<void> {
    const b64 = Buffer.from(JSON.stringify({ errorMessage, items })).toString("base64");
    this.seq += 1;
    await this.page.evaluate(([b, s, q]) => (window as any).renderTranscript(b, s, q, "snapshot"), [b64, stick, this.seq] as const);
  }

  /** What native does when the reader deliberately drags away from the bottom. */
  /** A reader drag of dy points (negative = toward older messages), applied the way a flick would. */
  async dragBy(dy: number): Promise<void> {
    await this.page.evaluate((delta) => {
      (window as any).setStickToBottom(false);
      (window as any).__s.user += delta;
      window.scrollBy(0, delta);
    }, dy);
  }

  async scrollAway(y: number): Promise<void> {
    await this.page.evaluate((target) => {
      (window as any).setStickToBottom(false);
      window.scrollTo(0, target);
    }, y);
    await this.settle(2);
  }

  /** Native re-asserting "pinned" (reader returned to the bottom). */
  async pin(): Promise<void> {
    await this.page.evaluate(() => {
      (window as any).setStickToBottom(true);
      window.scrollTo(0, document.documentElement.scrollHeight);
    });
    await this.settle(2);
  }

  async settle(frames = 3): Promise<void> {
    await this.page.evaluate(
      (n) => new Promise<void>((resolve) => {
        let left = n;
        const tick = () => (--left <= 0 ? resolve() : requestAnimationFrame(tick));
        requestAnimationFrame(tick);
      }),
      frames,
    );
  }

  /** Token of the first row whose top sits in the reading band of the viewport. */
  async pickAnchor(): Promise<string> {
    const token = await this.page.evaluate(() => (window as any).__s.pick());
    if (!token) throw new Error("no row in the reading band");
    return token;
  }

  /** Token of the topmost row still on screen: the one the page itself anchors to. */
  async pickTopAnchor(): Promise<string> {
    const token = await this.page.evaluate(() => (window as any).__s.pickTop());
    if (!token) throw new Error("no row at the top of the viewport");
    return token;
  }

  /** Set by the runner: a directory to film into while sampling. */
  filmDir: string | null = null;
  private filming: Promise<void> | null = null;
  private filmOn = false;
  filmFrames: string[] = [];

  async startSampling(token: string): Promise<void> {
    await this.page.evaluate((t) => (window as any).__s.start(t), token);
    if (this.filmDir && !this.filming) {
      mkdirSync(this.filmDir, { recursive: true });
      this.filmOn = true;
      this.filmFrames = [];
      this.filming = (async () => {
        while (this.filmOn && this.filmFrames.length < 400) {
          const file = path.join(this.filmDir!, `${String(this.filmFrames.length).padStart(3, "0")}.png`);
          try {
            await this.page.screenshot({ path: file });
            this.filmFrames.push(file);
          } catch {
            return;
          }
        }
      })();
    }
  }

  async stopSampling(): Promise<Frame[]> {
    const frames: Frame[] = await this.page.evaluate(() => (window as any).__s.stop());
    this.filmOn = false;
    await this.filming;
    this.filming = null;
    return frames;
  }

  /** One synchronous measurement, taken before WebKit has had a frame. */
  async snap(token: string): Promise<Frame> {
    return this.page.evaluate((t) => (window as any).__s.snap(t), token);
  }

  async screenshot(file: string): Promise<void> {
    await this.page.screenshot({ path: file });
  }
}

// Runs in the page. Rows are located by a token in their text so the harness
// works on any renderer version, including ones with no row ids in the DOM.
const SAMPLER = `
(() => {
  const root = () => document.getElementById("root");
  const rowOf = (token) => [...root().children].find((el) => el.textContent.includes(token));
  const frame = (token, t0) => {
    const el = token ? rowOf(token) : null;
    const maxY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    return {
      t: performance.now() - t0,
      top: el ? Math.round(el.getBoundingClientRect().top * 100) / 100 : null,
      y: Math.round(window.scrollY * 100) / 100,
      height: document.documentElement.scrollHeight,
      viewport: window.innerHeight,
      gap: Math.round((maxY - window.scrollY) * 100) / 100,
      user: s.user,
    };
  };
  const s = { frames: [], raf: 0, running: false, user: 0 };
  s.pick = () => {
    const el = [...root().children].find((e) => {
      const top = e.getBoundingClientRect().top;
      return top > 150 && top < 450 && /TOK_[AU]\\d+/.test(e.textContent);
    });
    return el ? el.textContent.match(/TOK_[AU]\\d+/)[0] : null;
  };
  s.pickTop = () => {
    const el = [...root().children].find((e) => e.getBoundingClientRect().bottom > 1 && /TOK_[AU]\\d+/.test(e.textContent));
    return el ? el.textContent.match(/TOK_[AU]\\d+/)[0] : null;
  };
  s.snap = (token) => frame(token, performance.now());
  s.start = (token) => {
    s.frames = [];
    s.user = 0;
    s.running = true;
    const t0 = performance.now();
    const loop = () => {
      if (!s.running) return;
      s.frames.push(frame(token, t0));
      s.raf = requestAnimationFrame(loop);
    };
    s.frames.push(frame(token, t0));
    s.raf = requestAnimationFrame(loop);
  };
  s.stop = () => {
    s.running = false;
    cancelAnimationFrame(s.raf);
    return s.frames;
  };
  window.__s = s;
})();
`;

// ---------- fixtures ----------

const para = (tag: string, n: number) =>
  Array.from({ length: n }, (_, i) => `Paragraph ${tag}-${i}: the quick brown fox jumps over the lazy dog while the build runs.`).join("\n\n");

export function userItem(i: number): Item {
  return { id: `u${i}`, kind: "message", role: "user", body: `TOK_U${i} please check the ${i}th thing` };
}

export interface AssistantOpts {
  code?: boolean;
  media?: "none" | "nodims" | "dims";
  paragraphs?: number;
}

export function assistantItem(i: number, o: AssistantOpts = {}): Item {
  const code = o.code === false ? "" : "\n\n```\n" + `const veryLongIdentifierName${i} = someFunction(argumentNumberOne, argumentNumberTwo, argumentNumberThree);` + "\n```";
  const media =
    o.media && o.media !== "none"
      ? [{ sha256: `img${i}`, url: `/m/img${i}.svg`, mimeType: "image/svg+xml", ...(o.media === "dims" ? { width: 600, height: 400 } : {}) }]
      : [];
  return {
    id: `a${i}`,
    kind: "message",
    role: "assistant",
    body: `TOK_A${i}\n\n${para(`R${i}`, o.paragraphs ?? 2)}${code}`,
    media,
  };
}

export function toolItem(i: number, state: "running" | "done", outputLines = 0): Item {
  return {
    id: `t${i}`,
    kind: "tool",
    title: `Bash ${i}`,
    subtitle: `TOK_T${i} npm test`,
    status: state === "running" ? "running" : "ok",
    duration: state === "done" ? "2.1s" : null,
    input: "npm test",
    output: state === "done" && outputLines ? Array.from({ length: outputLines }, (_, k) => `line ${k} of output`).join("\n") : null,
  };
}

/** A transcript of n exchanges: user, assistant (with optional media on every `mediaEvery`-th). */
export function conversation(n: number, o: { mediaEvery?: number; media?: "nodims" | "dims"; from?: number } = {}): Item[] {
  const out: Item[] = [];
  for (let i = o.from ?? 0; i < (o.from ?? 0) + n; i++) {
    out.push(userItem(i));
    const withMedia = o.mediaEvery && i % o.mediaEvery === 0;
    out.push(assistantItem(i, { media: withMedia ? o.media ?? "nodims" : "none" }));
  }
  return out;
}

/** Streaming text for the last assistant message: grows by one chunk per call. */
export function streamingBody(step: number): string {
  const chunks = [
    "Looking at the failure now.\n\n",
    "Here is what I found:\n\n- the cache key ignores the lockfile\n- the runner image changed\n",
    "- tests pass locally\n\n",
    "```ts\nexport function key(a: string) {\n  return a.trim();\n}\n",
    "const another = 1;\n```\n\n",
    "## Summary\n\nThe fix is a one line change. ",
    "| col | value |\n| --- | --- |\n| a | 1 |\n| b | 2 |\n\n",
    "Done.",
  ];
  let out = "";
  for (let i = 0; i <= step; i++) out += chunks[i % chunks.length] + (i >= chunks.length ? `(${i})` : "");
  return out;
}
