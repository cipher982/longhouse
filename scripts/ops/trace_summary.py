#!/usr/bin/env python3
"""Summarize an Instruments .trace for a terminal reader: where CPU went,
which of the app's own frames it went through, and any hangs or hitches.

    scripts/ops/trace_summary.py <file.trace> [--top 15]

A raw `xctrace export` is tens of thousands of XML lines with id/ref
de-duplication; this resolves the refs and prints a page.
"""
from __future__ import annotations

import argparse
import collections
import subprocess
import sys
import xml.etree.ElementTree as ET

APP_BINARIES = {"Longhouse", "Longhouse.debug.dylib", "LonghouseWidgets"}
# A sample whose innermost frame is one of these is a parked thread. Device
# templates weight such a sample by the whole time since the thread last ran,
# so counting them turns seconds of sleep into "CPU".
WAIT_LEAVES = {
    "start_wqthread", "mach_msg2_trap", "mach_msg_trap", "__psynch_cvwait",
    "semaphore_wait_trap", "__ulock_wait", "__ulock_wait2", "kevent_id",
    "kevent_qos", "__workq_kernreturn", "__semwait_signal", "__select",
}


def export(trace: str, schema: str) -> ET.Element | None:
    xpath = f'/trace-toc/run[@number="1"]/data/table[@schema="{schema}"]'
    proc = subprocess.run(
        ["xcrun", "xctrace", "export", "--input", trace, "--xpath", xpath],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or "<row" not in proc.stdout:
        return None
    return ET.fromstring(proc.stdout)


def rows(root: ET.Element):
    """Yield each row as a list of resolved elements (refs followed)."""
    by_id: dict[str, ET.Element] = {}

    def resolve(el: ET.Element) -> ET.Element:
        for sub in el.iter():
            if "id" in sub.attrib:
                by_id[sub.attrib["id"]] = sub
        ref = el.attrib.get("ref")
        return by_id.get(ref, el) if ref else el

    for row in root.iter("row"):
        yield [resolve(child) for child in row]


def frame_binary(frame: ET.Element, binaries: dict[str, str]) -> str:
    binary = frame.find("binary")
    if binary is None:
        return "?"
    if "id" in binary.attrib:
        binaries[binary.attrib["id"]] = binary.attrib.get("name", "?")
        return binaries[binary.attrib["id"]]
    return binaries.get(binary.attrib.get("ref", ""), "?")


def time_profile(trace: str, top: int, process: str | None, callers_of: str | None = None) -> None:
    root = export(trace, "time-profile")
    if root is None:
        print("time-profile: none (template without CPU sampling)")
        return
    frames: dict[str, ET.Element] = {}
    backtraces: dict[str, list[ET.Element]] = {}
    binaries: dict[str, str] = {}
    leaf = collections.Counter()
    app_frame = collections.Counter()
    threads = collections.Counter()
    callers = collections.Counter()
    total = 0
    for cols in rows(root):
        weight = 0
        thread_name = "?"
        running = True
        in_process = process is None
        stack: list[ET.Element] = []
        for col in cols:
            if col.tag == "process" and process is not None:
                in_process = col.attrib.get("fmt", "").startswith(f"{process} (")
            elif col.tag == "thread-state":
                # Templates that sample every thread (App Launch) include
                # blocked ones; only on-CPU time is cost.
                running = col.attrib.get("fmt", "Running") == "Running"
            elif col.tag == "weight":
                weight = int(col.text or 0)
            elif col.tag == "thread":
                fmt = col.attrib.get("fmt", "?")
                thread_name = "main" if fmt.startswith("Main Thread") else fmt.split(" (")[0]
            elif col.tag in ("tagged-backtrace", "backtrace"):
                bt = col.find("backtrace") if col.tag == "tagged-backtrace" else col
                bt = bt if bt is not None else col
                if "ref" in bt.attrib:
                    stack = backtraces.get(bt.attrib["ref"], [])
                else:
                    stack = []
                    for f in bt.iter("frame"):
                        if "ref" in f.attrib:
                            f = frames.get(f.attrib["ref"], f)
                        else:
                            frames[f.attrib.get("id", "")] = f
                        stack.append(f)
                    if "id" in bt.attrib:
                        backtraces[bt.attrib["id"]] = stack
        if not in_process or not stack or not running or stack[0].attrib.get("name") in WAIT_LEAVES:
            continue
        total += weight
        threads[thread_name] += weight
        leaf[stack[0].attrib.get("name", "?")] += weight
        for f in stack:
            if frame_binary(f, binaries) in APP_BINARIES:
                app_frame[f.attrib.get("name", "?")] += weight
                break
        if callers_of:
            names = [f.attrib.get("name", "?") for f in stack]
            hits = [i for i, name in enumerate(names) if callers_of in name]
            if hits:
                # The nearest app frames above the outermost hit, closures
                # and thunks skipped: who asked for this work.
                above = [
                    name for f, name in zip(stack[hits[-1] + 1:], names[hits[-1] + 1:])
                    if frame_binary(f, binaries) in APP_BINARIES
                    and not name.startswith(("closure", "partial apply", "thunk", "outlined", "merged", "$s"))
                ][:3]
                callers[" <- ".join(above) or "(no app frame)"] += weight

    ms = lambda ns: f"{ns / 1e6:8.1f} ms"
    print(f"CPU samples: {ms(total)} total")
    for name, w in threads.most_common(5):
        print(f"  {ms(w)}  thread {name}")
    print(f"\nTop self-time symbols:")
    for name, w in leaf.most_common(top):
        print(f"  {ms(w)}  {name[:140]}")
    print(f"\nTop app frames (innermost Longhouse frame on each sample):")
    for name, w in app_frame.most_common(top):
        print(f"  {ms(w)}  {name[:140]}")
    if callers_of:
        print(f"\nCallers of {callers_of}:")
        for chain, w in callers.most_common(top):
            print(f"  {ms(w)}  {chain[:200]}")


def intervals(trace: str, schema: str, label: str) -> None:
    root = export(trace, schema)
    if root is None:
        return
    found = list(rows(root))
    print(f"\n{label}: {len(found)}")
    for cols in found[:20]:
        print("  " + " | ".join(c.attrib.get("fmt", c.text or "")[:80] for c in cols if c.attrib.get("fmt") or c.text))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--process", help="only this process (for --all-processes traces)")
    parser.add_argument("--callers", metavar="SYMBOL", help="also attribute samples containing SYMBOL to the app frames that called it")
    args = parser.parse_args()
    time_profile(args.trace, args.top, args.process, args.callers)
    intervals(args.trace, "potential-hangs", "Potential hangs")
    intervals(args.trace, "hitches", "Animation hitches")


if __name__ == "__main__":
    sys.exit(main())
