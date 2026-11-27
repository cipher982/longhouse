#!/usr/bin/env python3
"""Write a candidate terminal-truth-providers.toml with each provider's latest release.

    provider_latest.py OUT.toml   prints one `name old -> new` line per change, writes OUT.toml

npm providers: `npm view <package> version`. omp: newest GitHub release whose tag is exactly vX.Y.Z.
antigravity is left at its pin: its vendor bucket publishes no release listing or checksum to read.
A pin with `hold = "<reason>"` is left as it is and the reason printed.
"""

import json
import re
import subprocess
import sys

from terminal_truth_providers import PINS, load_pins

OMP_REPO = "can1357/oh-my-pi"
STABLE = re.compile(r"\d+\.\d+\.\d+")


def run(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def latest(pin: dict[str, str]) -> str | None:
    if pin["kind"] == "npm":
        return run("npm", "view", pin["package"], "version")
    if pin["kind"] == "omp":
        tags = json.loads(run("gh", "api", f"repos/{OMP_REPO}/releases?per_page=30"))
        return next((t["tag_name"][1:] for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t["tag_name"]) and not t["prerelease"]), None)
    return None


def main() -> int:
    text, changes = PINS.read_text(), []
    for name, pin in load_pins().items():
        if pin.get("hold"):
            print(f"{name} {pin['version']} (held: {pin['hold']})")
            continue
        new = latest(pin)
        if new is None:
            print(f"{name} {pin['version']} (not checked)")
        elif not STABLE.fullmatch(new):
            sys.exit(f"{name}: unexpected latest version {new!r}")
        elif new != pin["version"]:
            head, sep, rest = text.partition(f"\n[{name}]\n")
            body, sep2, tail = rest.partition("\n[")
            text = head + sep + body.replace(pin["version"], new) + sep2 + tail
            changes.append(name)
            print(f"{name} {pin['version']} -> {new}")
        else:
            print(f"{name} {new} (current)")
    open(sys.argv[1], "w").write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
