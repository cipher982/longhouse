# Runs inside `agent-browser run` (helpers such as js, press_key, capture_screenshot are pre-imported). web-paint-e2e.py prepends
# `CONFIG = {...}`. It opens the session page, samples the DOM while the turn streams and writes CONFIG["result_file"].
import json
import os
import time

C = CONFIG
OUT = C["out_dir"]
label = C["label"]
scenario = C["scenario"]
failures = []
shots = {}
t0 = time.monotonic()

SNAP = r"""(() => {
  const q = (s, root = document) => [...root.querySelectorAll(s)];
  const txt = (e) => (e ? (e.innerText || "").trim().replace(/\s+/g, " ") : "");
  return JSON.stringify({
    href: location.href,
    docW: document.documentElement.scrollWidth, winW: window.innerWidth,
    thinking: q('[data-testid="session-timeline-reasoning"][data-live="true"]').map((e) => ({
      id: e.id, expanded: e.classList.contains("is-expanded"), text: txt(e),
      body: txt(e.querySelector('[data-testid="session-timeline-reasoning-body"]')),
      ariaExpanded: e.querySelector("button")?.getAttribute("aria-expanded"), stored: localStorage.getItem("longhouse.liveThinking.expanded")})),
    storedThinking: q('[data-testid="session-timeline-reasoning"]:not([data-live="true"])').map((e) => txt(e).slice(0, 160)),
    run: q(".tl-run").map((e) => ({id: e.id, state: e.dataset.runState, headline: txt(e.querySelector(".tl-run__headline")),
      command: txt(e.querySelector(".tl-run__command")), tool: txt(e.querySelector('[data-testid="running-call-tool"]')), tail: (e.querySelector(".tl-run__tail")?.innerText || ""), full: txt(e)})),
    rows: q("[data-row-kind]").map((e) => ({id: e.id, kind: e.dataset.rowKind, live: e.dataset.live || "", text: txt(e).slice(0, 200)})),
    // The state strip: header pill and composer ledger on desktop; at phone width the header only shows a dot, so the composer's own text counts.
    ribbon: [...["session-header-state", "session-control-strip", "live-work-ribbon", "session-activity-strip"]
      .map((id) => txt(document.querySelector(`[data-testid="${id}"]`))), txt(document.querySelector('[class*="composer"]')).slice(0, 160)].filter(Boolean).join(" || "),
    body: document.body.innerText,
  });
})()"""


def snap():
    return json.loads(js(SNAP))


def shot(name):
    path = f"{OUT}/{label}-{name}.png"
    capture_screenshot(path)
    shots[name] = path


cdp("Emulation.setDeviceMetricsOverride", width=C["width"], height=C["height"], deviceScaleFactor=1, mobile=C["width"] < 600)
cdp("Page.enable")
# Diagnostic: what the page's own event stream delivered, so a row that vanishes can be tied to the frame that caused it.
cdp("Page.addScriptToEvaluateOnNewDocument", source="""
window.__sse = [];
const NativeES = window.EventSource;
window.EventSource = function (...args) {
  const es = new NativeES(...args);
  es.addEventListener("workspace_changed", (e) => {
    try {
      const d = JSON.parse(e.data);
      window.__sse.push([Math.round(performance.now()), d.change_kind || "", d.transcript_preview === undefined ? "absent" : d.transcript_preview === null ? "null" : d.transcript_preview.kind || "?"]);
    } catch (_) {}
  });
  return es;
};
window.EventSource.prototype = NativeES.prototype;
""")
goto_url(C["url"])
wait_for_load()
try:
    wait_for_js("document.querySelector('.session-runtime-strip, [data-testid=\"session-timeline-row\"], [data-row-kind]') !== null")
except Exception as exc:  # noqa: BLE001
    failures.append(f"session page did not render: {exc}")
# Start from a collapsed thinking preference so "collapsed by default" is what a new viewer sees.
js("localStorage.removeItem('longhouse.liveThinking.expanded'); window.__keys = []; window.addEventListener('keydown', (e) => window.__keys.push(e.key), true); 0")
time.sleep(1.0)  # the event stream connects
shot("before")
open(C["ready_file"], "w").write("ready")

first = {}  # what -> seconds since ready
samples = []
ribbons = []
tool_names = set()
run_texts = set()
row_tools = set()
max_run_rows = 0
MID_AFTER_S = 3.5
pressed = False
expanded_seen = collapsed_after_toggle = False
mid_shot = False
thinking_before_press = None
ready_t = time.monotonic()
# Cells whose provider streams a running command's output (spec Part 3: OMP and Pi tool_output live).
TAIL_CELLS = {("omp", "helm"), ("omp", "console"), ("pi", "helm")}
markers = C["markers"]
reply_words = markers["reply"]
tail_words = markers["tool_output"]


def seen(key):
    first.setdefault(key, round(time.monotonic() - ready_t, 2))


while not os.path.exists(C["stop_file"]) and time.monotonic() - ready_t < 120:
    s = snap()
    samples.append({"t": round(time.monotonic() - ready_t, 2), "thinking": len(s["thinking"]), "run": len(s["run"]), "ribbon": s["ribbon"]})
    if s["ribbon"] and (not ribbons or ribbons[-1] != s["ribbon"]):
        ribbons.append(s["ribbon"])
    if s["thinking"]:
        seen("thinking_row")
        row = s["thinking"][0]
        if not pressed and not C.get("no_press"):
            thinking_before_press = row
            if not mid_shot:
                shot("mid-thinking-collapsed")
                mid_shot = True
            press_key("t")
            pressed = True
            trace = []
            for _ in range(8):
                time.sleep(0.15)
                st = snap()["thinking"]
                trace.append([[(r["expanded"], r["stored"], bool(r["body"])) for r in st], js("[Math.round(performance.now()), window.__keys, document.activeElement && document.activeElement.tagName]")])
            first["press_trace"] = trace
            try:
                wait_for_js("document.querySelector('[data-testid=\"session-timeline-reasoning\"][data-live=\"true\"].is-expanded') !== null", timeout=3)
                s2 = snap()
                expanded_seen = bool(s2["thinking"] and s2["thinking"][0]["expanded"] and s2["thinking"][0]["body"])
                seen("thinking_expanded")
                shot("mid-thinking-expanded")
                first["thinking_body"] = (s2["thinking"][0]["body"] if s2["thinking"] else "")[:200]
            except Exception:  # noqa: BLE001
                pass
            press_key("t")
            time.sleep(0.3)
            s3 = snap()
            collapsed_after_toggle = bool(s3["thinking"]) and not s3["thinking"][0]["expanded"] and s3["thinking"][0]["stored"] == "0"
            if not s3["thinking"]:
                collapsed_after_toggle = True  # the row ended while we pressed; the second press still stored the choice
    if scenario == "subagent" and ("TtSub" if C["provider"] == "omp" else markers["subagent_started"][0]) in s["body"]:
        seen("subagent_label")
    if len(s["run"]) > max_run_rows:
        max_run_rows = len(s["run"])
    for r in s["run"]:
        seen("run_row")
        tool_names.add(r["headline"] + " | " + r["command"])
        if r["tool"]:
            row_tools.add(r["tool"])
        run_texts.add(r["full"][:200])
        if any(w and w in r["tail"] for w in tail_words):
            seen("tool_tail")
        # The shot is taken a few seconds into the call, so what is drawn is the settled running row, not its first frame.
        first.setdefault("mid_run_row", r)
    started_at = first.get("run_row", first.get("subagent_label"))
    if scenario in ("tool", "subagent") and not mid_shot and started_at is not None and time.monotonic() - ready_t >= started_at + MID_AFTER_S:
        shot("mid-" + scenario)
        mid_shot = True
        try:
            r = cdp("Runtime.evaluate", expression="fetch('/api/agents/sessions/' + location.pathname.split('/').pop() + '/events?limit=100').then(r => r.json()).then(j => JSON.stringify((j.events || []).filter(e => e.tool_name).map(e => [e.id, e.role, e.tool_name, e.tool_call_id, e.tool_call_state])))", awaitPromise=True, returnByValue=True)
            first["durable_tool_events"] = r.get("result", {}).get("value")
        except Exception as exc:  # noqa: BLE001
            first["durable_tool_events"] = f"error {exc}"
        first["mid_run_ids"] = [(r["id"], r["headline"][:30], r["state"]) for r in s["run"]]
        first["mid_rows"] = [(r["kind"], r["live"], r["text"][:100]) for r in s["rows"]][-4:]
    time.sleep(C.get('interval', 0.2))

# The turn is over (harness saw the final durable event). Give the page its settle time, then look again.
time.sleep(2.5)
final = snap()
shot("after")
body = final["body"]
dups = sorted({r["id"] for r in final["rows"] if r["id"] and [x["id"] for x in final["rows"]].count(r["id"]) > 1})
marker_counts = {w: body.count(w) for w in reply_words if w}

# ---- assertions -----------------------------------------------------------------------------------------------------
if final["docW"] > final["winW"] + 1:
    failures.append(f"horizontal overflow: document {final['docW']}px wide in a {final['winW']}px window")
if dups:
    failures.append(f"duplicate row ids after the turn: {dups}")
if final["thinking"]:
    failures.append(f"live thinking row still on the page after the turn: {final['thinking']}")
stuck = [r for r in final["run"] if r["state"] in ("live",)]
if stuck:
    failures.append(f"running call row stuck after the turn: {[r['headline'] for r in stuck]}")
for w, n in marker_counts.items():
    if n > 1:
        failures.append(f"reply marker {w} is drawn {n} times after the turn")
if reply_words and not any(n >= 1 for n in marker_counts.values()):
    failures.append("durable reply never reached the page after the turn")

if max_run_rows > 1:
    failures.append(f"{max_run_rows} running-call rows at once for a turn with one call (the live preview drew a second row for the durable call)")
if scenario == "thinking":
    if "thinking_row" not in first:
        failures.append("no live Thinking row appeared during the turn")
    elif not C.get("no_press"):
        row = thinking_before_press
        if row["expanded"] or row["body"]:
            failures.append(f"thinking row was not collapsed by default: {row}")
        if "Thinking" not in row["text"]:
            failures.append(f"thinking row label is not 'Thinking': {row['text']!r}")
        if not expanded_seen:
            failures.append("t did not expand the thinking row with its text")
        if not collapsed_after_toggle:
            failures.append("a second t did not collapse the thinking row")
elif scenario == "tool":
    want = {"claude": "Bash", "antigravity": "run_command", "omp": "bash", "pi": "bash", "codex": "exec"}.get(C["provider"], "")
    if "run_row" not in first:
        failures.append("no running tool row appeared during the turn")
    else:
        # The row names its tool in a label, or is headlined by it when it has nothing better. Antigravity's display name for run_command is
        # "Shell"; the state strip carries the raw name.
        row_name = {"antigravity": "shell"}.get(C["provider"], want.lower())
        named = {t.lower() for t in row_tools} | {t.split(" | ")[0].lower() for t in tool_names}
        if row_name not in named:
            failures.append(f"the running row does not name its tool {row_name!r}: {sorted(named)}")
        if want and not any(want.lower() in b.lower() for b in ribbons):
            failures.append(f"the state strip never said it was using {want!r}: {ribbons}")
    if "run_row" in first and "tool_tail" not in first and (C["provider"], C["mode"]) in TAIL_CELLS:
        failures.append("running tool row never showed any of the command's output lines")
elif scenario == "subagent":
    # Claude carries the spawn marker in the agent description; OMP's label is the task's agent name.
    spawn = "TtSub" if C["provider"] == "omp" else markers["subagent_started"][0]
    if "subagent_label" not in first:
        failures.append(f"the subagent label ({spawn}) was never on the page while the subagent ran, without expanding anything")
    if not any(w in b.lower() for b in ribbons for w in ("agent", "task", "background")):
        failures.append(f"the state strip never named the subagent work: {ribbons}")

result = {
    "ok": not failures,
    "failures": failures,
    "shots": shots,
    "first_seen_s": first,
    "max_run_rows": max_run_rows,
    "provider_mode": [C["provider"], C["mode"]],
    "ribbon_texts": ribbons,
    "run_rows_seen": sorted(tool_names),
    "run_row_texts": sorted(run_texts),
    "row_tool_labels": sorted(row_tools),
    "marker_counts_after": marker_counts,
    "final_rows": [(r["kind"], r["live"], r["text"][:80]) for r in final["rows"]][-12:],
    "stored_thinking_after": final["storedThinking"],
    "overflow": [final["docW"], final["winW"]],
    "timeline": [samples[i] for i in range(len(samples)) if i == 0 or (samples[i]["thinking"], samples[i]["run"], samples[i]["ribbon"]) != (samples[i - 1]["thinking"], samples[i - 1]["run"], samples[i - 1]["ribbon"])],
    "durable_events_after": json.loads(cdp("Runtime.evaluate", expression="fetch('/api/agents/sessions/' + location.pathname.split('/').pop() + '/events?limit=100').then(r => r.json()).then(j => JSON.stringify((j.events || []).map(e => [e.role, e.interaction_kind || '', e.timestamp])))", awaitPromise=True, returnByValue=True).get("result", {}).get("value") or "[]"),
    "sse_seen": json.loads(js("JSON.stringify((window.__sse || []).slice(0, 60))")),
    "page_now": js("Math.round(performance.now())"),
    "samples": len(samples),
}
open(C["result_file"], "w").write(json.dumps(result))
print(json.dumps(result, indent=1))
