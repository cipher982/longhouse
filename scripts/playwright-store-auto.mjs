// Side-effect import: make Chromium available to this process, instantly when
// it is already installed. Put it FIRST in any script or Playwright config that
// launches a browser, before anything imports playwright:
//
//   import "../playwright-store-auto.mjs";
//
// It points Playwright at Longhouse's private browser store (see
// playwright-store.mjs for why) and, only if Chromium is missing, installs it
// once with a bounded, visible, cleaned-up installer. scripts/tests/
// playwright-store.test.mjs fails if a browser-launching script omits it.
import { ensureBrowsersSync } from "./playwright-store.mjs";

ensureBrowsersSync(["chromium"]);
