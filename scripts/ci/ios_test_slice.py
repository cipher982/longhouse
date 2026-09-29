#!/usr/bin/env python3
"""Print the -only-testing arguments for one slice of an xcodebuild test enumeration.

    xcodebuild ... -enumerate-tests -test-enumeration-style flat \
        -test-enumeration-format json -test-enumeration-output-path tests.json \
        test-without-building
    ios_test_slice.py tests.json 2/3

Slice i of n takes every n-th enabled test, in sorted order, starting at the i-th.
Sorting puts a class's tests next to each other and the stride spreads them over
the slices, which balances the slow SessionChatUITests class (28 of the 35 UI
tests) to within 7% on n=2. Any set of n slices covers every test exactly once.
"""

from __future__ import annotations

import json
import sys


def parse_slice(spec: str) -> tuple[int, int]:
    index, slash, count = spec.partition("/")
    if not (slash and index.isdigit() and count.isdigit()):
        raise ValueError(f"IOS_TEST_SLICE must look like 1/2, got {spec!r}")
    i, n = int(index), int(count)
    if not 1 <= i <= n:
        raise ValueError(f"IOS_TEST_SLICE {spec!r}: need 1 <= i <= n")
    return i, n


def enabled_tests(enumeration: dict) -> list[str]:
    if enumeration.get("errors"):
        raise ValueError(f"test enumeration reported errors: {enumeration['errors']}")
    identifiers = sorted(
        {
            test["identifier"]
            for value in enumeration.get("values", [])
            for test in value.get("enabledTests", [])
        }
    )
    if not identifiers:
        raise ValueError("test enumeration listed no enabled tests")
    return identifiers


def slice_arguments(enumeration: dict, spec: str) -> list[str]:
    i, n = parse_slice(spec)
    return [
        f"-only-testing:{identifier}"
        for position, identifier in enumerate(enabled_tests(enumeration))
        if position % n == i - 1
    ]


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        with open(sys.argv[1], encoding="utf-8") as handle:
            arguments = slice_arguments(json.load(handle), sys.argv[2])
    except (OSError, ValueError) as error:
        print(f"ios_test_slice: {error}", file=sys.stderr)
        return 1
    print("\n".join(arguments))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
