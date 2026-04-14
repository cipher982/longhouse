#!/usr/bin/env python3
import re
import subprocess
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: select_ios_simulator.py <project> <scheme>", file=sys.stderr)
        return 2

    project, scheme = sys.argv[1], sys.argv[2]
    output = subprocess.check_output(
        [
            "xcodebuild",
            "-project",
            project,
            "-scheme",
            scheme,
            "-showdestinations",
        ],
        text=True,
    )

    for line in output.splitlines():
        if "platform:iOS Simulator" not in line or "name:iPhone" not in line or "placeholder" in line:
            continue
        name_match = re.search(r"name:([^,}]+)", line)
        os_match = re.search(r"OS:([^,}]+)", line)
        if name_match and os_match:
            name = name_match.group(1).strip()
            os_version = os_match.group(1).strip()
            print(f"platform=iOS Simulator,OS={os_version},name={name}")
            return 0

    print("No iPhone simulator destination found", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
