// Stands in for @xterm/xterm and @xterm/addon-fit while prerendering: both are
// one UMD file that reads `self` on import, so Node cannot load them, and a
// terminal is only ever constructed in an effect, which prerender never runs.
export class Terminal {}
export class FitAddon {}
