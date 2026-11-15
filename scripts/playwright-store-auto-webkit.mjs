// Same as playwright-store-auto.mjs, for scripts that drive WebKit.
import { ensureBrowsersSync } from "./playwright-store.mjs";

ensureBrowsersSync(["webkit"]);
