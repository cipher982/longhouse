#!/usr/bin/env python3
"""Summarize an Instruments .trace for a terminal reader: where CPU went,
which of the app's own frames it went through, and any hangs or hitches.

    scripts/ops/trace_summary.py <file.trace> [--top 15] [--process Longhouse]
                                 [--callers SYMBOL]

A raw `xctrace export` is tens of thousands of XML lines with id/ref
de-duplication; this resolves the refs and prints a page. The export goes to
a file and is parsed as a stream, keeping only small lookup tables: an
all-processes trace of a few minutes is gigabytes as a tree, which swapped
the 8 GB bench until it dropped off the network.
"""
from __future__ import annotations

import argparse
import collections
import os
import subprocess
import sys
import tempfile
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
SKIPPED_CALLER_PREFIXES = ("closure", "partial apply", "thunk", "outlined", "merged", "$s")
# App frames Instruments left as bare addresses: name -> (binary, load address).
UNRESOLVED: dict[str, tuple[str, str]] = {}


def export(trace: str, schema: str, directory: str) -> str | None:
    """Export one table to a file; None when the trace has no such rows."""
    path = os.path.join(directory, f"{schema}.xml")
    xpath = f'/trace-toc/run[@number="1"]/data/table[@schema="{schema}"]'
    proc = subprocess.run(
        ["xcrun", "xctrace", "export", "--input", trace, "--xpath", xpath, "--output", path],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        if b"<row" not in handle.read(1 << 20):
            return None
    return path


def stream_rows(path: str):
    """Yield each row as {column tag: (fmt, text)}, plus "stack": a list of
    (frame name, binary name), innermost first, for a backtrace column.

    Values are kept per id as small tuples, never elements, so the tree is
    cleared as it goes.
    """
    plain: dict[str, tuple[str, str]] = {}
    binaries: dict[str, str] = {}
    loads: dict[str, str] = {}
    frames: dict[str, tuple[str, str]] = {}
    backtraces: dict[str, list[tuple[str, str]]] = {}
    # A repeated <tagged-backtrace ref=N/> names the tagged-backtrace's own
    # id, not its inner backtrace's; resolving it through `backtraces`
    # silently dropped every repeated stack.
    tagged: dict[str, list[tuple[str, str]]] = {}
    parents: list[ET.Element] = []

    def frame_of(el: ET.Element) -> tuple[str, str]:
        ref = el.attrib.get("ref")
        if ref:
            return frames.get(ref, ("?", "?"))
        return frames.get(el.attrib.get("id", ""), (el.attrib.get("name", "?"), "?"))

    def backtrace_of(el: ET.Element) -> list[tuple[str, str]]:
        ref = el.attrib.get("ref")
        if ref:
            return backtraces.get(ref, [])
        return backtraces.get(el.attrib.get("id", ""), [frame_of(f) for f in el.findall("frame")])

    def tagged_stack(el: ET.Element) -> list[tuple[str, str]]:
        # Frames sit either in an inner <backtrace> or directly in the tag.
        inner = el.find("backtrace")
        return backtrace_of(inner) if inner is not None else [frame_of(f) for f in el.findall("frame")]

    for event, el in ET.iterparse(path, events=("start", "end")):
        if event == "start":
            parents.append(el)
            continue
        parents.pop()
        tag = el.tag
        if tag == "binary" and "id" in el.attrib:
            binaries[el.attrib["id"]] = el.attrib.get("name", "?")
            loads[el.attrib["id"]] = el.attrib.get("load-addr", "")
        elif tag == "frame" and "id" in el.attrib:
            binary = el.find("binary")
            name = "?"
            load = ""
            if binary is not None:
                key = binary.attrib.get("id") or binary.attrib.get("ref", "")
                name = binaries.get(key, "?")
                load = loads.get(key, "")
            frame_name = el.attrib.get("name", "?")
            if frame_name.startswith("0x") and name in APP_BINARIES and load:
                UNRESOLVED.setdefault(frame_name, (name, load))
            frames[el.attrib["id"]] = (frame_name, name)
        elif tag == "backtrace" and "id" in el.attrib:
            backtraces[el.attrib["id"]] = [frame_of(f) for f in el.findall("frame")]
        elif tag == "tagged-backtrace" and "id" in el.attrib:
            tagged[el.attrib["id"]] = tagged_stack(el)
        elif tag == "row":
            out: dict[str, object] = {}
            for col in el:
                if col.tag == "tagged-backtrace":
                    ref = col.attrib.get("ref")
                    out["stack"] = tagged.get(ref, []) if ref else tagged.get(col.attrib.get("id", ""), tagged_stack(col))
                    continue
                if col.tag == "backtrace":
                    out["stack"] = backtrace_of(col)
                    continue
                ref = col.attrib.get("ref")
                if ref:
                    out[col.tag] = plain.get(ref, ("", ""))
                else:
                    value = (col.attrib.get("fmt", ""), (col.text or "").strip())
                    if "id" in col.attrib:
                        plain[col.attrib["id"]] = value
                    out[col.tag] = value
            yield out
            # Drop the rows parsed so far; ids live on in the tables above.
            if parents:
                parents[-1].clear()
        elif "id" in el.attrib and tag != "row":
            plain.setdefault(el.attrib["id"], (el.attrib.get("fmt", ""), (el.text or "").strip()))


def resolve_addresses(dsym: str | None) -> dict[str, str]:
    """atos the app addresses Instruments could not name, against the build's
    .dSYM. symbolicate misses some frames even with the right dSYM."""
    if not dsym or not UNRESOLVED:
        return {}
    names: dict[str, str] = {}
    by_binary: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for address, key in UNRESOLVED.items():
        by_binary[key].append(address)
    for (binary, load), addresses in by_binary.items():
        dwarf = os.path.join(dsym, "Contents", "Resources", "DWARF", binary)
        if not os.path.exists(dwarf):
            continue
        out = subprocess.run(
            ["xcrun", "atos", "-o", dwarf, "-arch", "arm64", "-l", load, *addresses],
            capture_output=True, text=True,
        ).stdout.splitlines()
        for address, line in zip(addresses, out):
            if line and not line.startswith("0x"):
                names[address] = line.split(" (in ")[0]
    return names


def time_profile(trace: str, directory: str, top: int, process: str | None, callers_of: str | None,
                 dsym: str | None = None) -> None:
    path = export(trace, "time-profile", directory)
    if path is None:
        print("time-profile: none (template without CPU sampling)")
        return
    leaf = collections.Counter()
    app_frame = collections.Counter()
    threads = collections.Counter()
    callers = collections.Counter()
    inclusive = collections.Counter()
    app_total = 0
    total = 0
    names: dict[str, str] = {}
    if dsym:
        for _ in stream_rows(path):  # collect the addresses to name first
            pass
        names = resolve_addresses(dsym)
    for row in stream_rows(path):
        stack = row.get("stack") or []
        if names:
            stack = [(names.get(name, name), binary) for name, binary in stack]
        if not stack or stack[0][0] in WAIT_LEAVES:
            continue
        # Templates that sample every thread (App Launch) include blocked
        # ones; only on-CPU time is cost.
        if row.get("thread-state", ("Running", ""))[0] not in ("Running", ""):
            continue
        if process is not None and not row.get("process", ("", ""))[0].startswith(f"{process} ("):
            continue
        try:
            weight = int(row.get("weight", ("", "0"))[1] or 0)
        except ValueError:
            continue
        thread_fmt = row.get("thread", ("?", ""))[0]
        thread = "main" if thread_fmt.startswith("Main Thread") else thread_fmt.split(" (")[0]
        total += weight
        threads[thread] += weight
        leaf[stack[0][0]] += weight
        app_names = [name for name, binary in stack if binary in APP_BINARIES]
        if app_names:
            app_frame[app_names[0]] += weight
            # Samples with no app frame anywhere are system or simulator
            # overhead the app did not ask for; everything else is the
            # app's cost, and inclusive time ranks where it went.
            deep = [n for n in app_names if n not in ("main", "static LonghouseApp.$main()", "__debug_main_executable_dylib_entry_point")]
            if deep:
                app_total += weight
                for name in set(n for n in deep if not n.startswith(SKIPPED_CALLER_PREFIXES)):
                    inclusive[name] += weight
        if callers_of:
            hits = [i for i, (name, _) in enumerate(stack) if callers_of in name]
            if hits:
                # The nearest app frames above the outermost hit, closures
                # and thunks skipped: who asked for this work.
                above = [
                    name for name, binary in stack[hits[-1] + 1:]
                    if binary in APP_BINARIES and not name.startswith(SKIPPED_CALLER_PREFIXES)
                ][:3]
                callers[" <- ".join(above) or "(no app frame)"] += weight

    ms = lambda ns: f"{ns / 1e6:8.1f} ms"
    print(f"CPU samples: {ms(total)} total")
    for name, w in threads.most_common(5):
        print(f"  {ms(w)}  thread {name}")
    print("\nTop self-time symbols:")
    for name, w in leaf.most_common(top):
        print(f"  {ms(w)}  {name[:140]}")
    print(f"\nApp-attributable CPU (an app frame below main on the stack): {ms(app_total)}")
    for name, w in inclusive.most_common(top):
        print(f"  {ms(w)}  {name[:140]}")
    print("\nTop app frames (innermost Longhouse frame on each sample):")
    for name, w in app_frame.most_common(top):
        print(f"  {ms(w)}  {name[:140]}")
    if callers_of:
        print(f"\nCallers of {callers_of}:")
        for chain, w in callers.most_common(top):
            print(f"  {ms(w)}  {chain[:200]}")


def intervals(trace: str, directory: str, schema: str, label: str, process: str | None) -> None:
    path = export(trace, schema, directory)
    if path is None:
        return
    found = []
    for row in stream_rows(path):
        values = [fmt or text for key, (fmt, text) in row.items() if key != "stack"]
        if process is not None and not any(v.startswith(f"{process} (") for v in values):
            continue
        found.append(values)
    print(f"\n{label}: {len(found)}")
    for values in found[:20]:
        print("  " + " | ".join(v[:80] for v in values if v))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--process", help="only this process (for --all-processes traces)")
    parser.add_argument("--callers", metavar="SYMBOL", help="also attribute samples containing SYMBOL to the app frames that called it")
    parser.add_argument("--dsym", help="the app's .dSYM, to name frames Instruments left as addresses")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="trace-summary-") as directory:
        time_profile(args.trace, directory, args.top, args.process, args.callers, args.dsym)
        intervals(args.trace, directory, "potential-hangs", "Potential hangs", args.process)
        intervals(args.trace, directory, "hitches", "Animation hitches", args.process)


if __name__ == "__main__":
    sys.exit(main())
