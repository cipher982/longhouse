#!/usr/bin/env python3
"""
Hard validator for patrol findings.
Rejects anything without concrete evidence.
"""

import json
import re
import sys
from pathlib import Path

# Evidence patterns
FILE_LINE_PATTERN = re.compile(r'[a-zA-Z0-9_/.-]+\.(py|ts|tsx|js):\d+')
NO_FINDINGS = "NO_FINDINGS"


def validate_finding(finding: dict) -> tuple[bool, list[str]]:
    """
    Validate a patrol finding. Returns (valid, errors).

    Required fields:
    - evidence: list of file:line references
    - description: what the issue is
    - category: bug | doc_mismatch | edge_case | race | dead_code

    Optional:
    - test_snippet: failing test code
    - suggested_fix: 1-2 bullets
    """
    errors = []

    # Check for NO_FINDINGS (valid outcome)
    if finding.get("status") == NO_FINDINGS:
        return True, []

    # Required: evidence with file:line
    evidence = finding.get("evidence", [])
    if not evidence:
        errors.append("Missing evidence field")
    else:
        valid_refs = [e for e in evidence if FILE_LINE_PATTERN.search(str(e))]
        if not valid_refs:
            errors.append(f"No valid file:line references in evidence. Got: {evidence}")

    # Required: description
    if not finding.get("description"):
        errors.append("Missing description")

    # Required: category
    valid_categories = {"bug", "doc_mismatch", "edge_case", "race", "dead_code", "copy_paste"}
    category = finding.get("category")
    if not category:
        errors.append("Missing category")
    elif category not in valid_categories:
        errors.append(f"Invalid category '{category}'. Must be one of: {valid_categories}")

    # Verify evidence files exist
    repo_root = Path(__file__).parent.parent.parent
    for ref in evidence:
        match = FILE_LINE_PATTERN.search(str(ref))
        if match:
            file_part = match.group(0).split(":")[0]
            # Check common locations
            candidates = [
                repo_root / file_part,
                repo_root / "apps/zerg/backend" / file_part,
                repo_root / "apps/zerg/frontend-web/src" / file_part,
            ]
            if not any(c.exists() for c in candidates):
                errors.append(f"Evidence file not found: {file_part}")

    return len(errors) == 0, errors


def main():
    """CLI: reads JSON finding from stdin or file arg."""
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)

    valid, errors = validate_finding(data)

    if valid:
        print("VALID")
        sys.exit(0)
    else:
        print("INVALID")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
