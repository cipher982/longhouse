import { readFileSync } from "node:fs";
import path from "node:path";
import { defineConfig, type Plugin } from "vite";

// Builds the iOS transcript document (web/src/embeds/ios-transcript) into one
// self-contained, unminified HTML file: ios/Resources/Transcript/transcript.html.
// The app loads it with loadHTMLString and a Runtime Host base URL, so it can
// fetch nothing of its own: every script and style is inlined here. The file
// is checked in like the OpenAPI SDK, so iOS builds never need bun.
//
//   bun run build:ios-transcript            regenerate the checked-in file
//   make validate-ios-transcript            fail when it is stale

const embedRoot = path.resolve(import.meta.dirname, "src/embeds/ios-transcript");
export const transcriptOutDir = path.resolve(import.meta.dirname, "../ios/Resources/Transcript");
export const transcriptFileName = "transcript.html";

/// The palette marker the app replaces with TranscriptPalette.cssRootBlock.
/// It must reach the output verbatim, as the first thing in the first style.
export const rootBlockMarker = "/* __LH_ROOT_BLOCK__ */";

function inlineIntoSingleHtml(): Plugin {
  return {
    name: "ios-transcript-single-file",
    enforce: "post",
    generateBundle(_options, bundle) {
      const html = Object.values(bundle).find((entry) => entry.type === "asset" && entry.fileName.endsWith(".html"));
      if (!html || html.type !== "asset") throw new Error("ios-transcript: no HTML entry in the bundle");
      let document = String(html.source);
      // The stylesheet goes in verbatim, not through Vite's CSS pipeline: the
      // app splices its palette in at the marker with a plain string replace.
      const css = readFileSync(path.join(embedRoot, "transcript.css"), "utf8").trim();
      if (!css.startsWith(rootBlockMarker)) {
        throw new Error(`ios-transcript: transcript.css must start with ${rootBlockMarker}`);
      }
      // A function replacement: `$&` and friends in the source stay literal.
      document = document.replace("</head>", () => `  <style>\n${css}\n  </style>\n</head>`);
      for (const [fileName, entry] of Object.entries(bundle)) {
        if (entry === html) continue;
        if (entry.type !== "chunk") {
          throw new Error(`ios-transcript: unexpected asset ${fileName}; the document must stay self-contained`);
        }
        const escaped = fileName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        document = document.replace(new RegExp(`\\s*<script[^>]*src="[^"]*${escaped}"[^>]*></script>`), "");
        // Classic, not module: the document ran as one synchronous script at
        // the end of <body>, and WebKit calls its globals after didFinish.
        document = document.replace("</body>", () => `  <script>\n${entry.code.trim()}\n  </script>\n</body>`);
        delete bundle[fileName];
      }
      html.fileName = transcriptFileName;
      html.source = document;
    },
  };
}

export default defineConfig({
  root: embedRoot,
  base: "./",
  logLevel: "warn",
  plugins: [inlineIntoSingleHtml()],
  build: {
    outDir: transcriptOutDir,
    emptyOutDir: true,
    minify: false,
    cssMinify: false,
    modulePreload: false,
    sourcemap: false,
    assetsInlineLimit: Number.MAX_SAFE_INTEGER,
    rolldownOptions: {
      input: path.join(embedRoot, "index.html"),
      output: {
        // One classic script: no imports survive bundling, so an IIFE keeps
        // every helper out of the page's global scope.
        format: "iife",
      },
    },
  },
});
