#!/usr/bin/env python3
"""Clean up commis-specific test database files created during E2E tests."""

import glob
import os
import sys


def cleanup_test_databases():
    """Remove all test database files from the backend directory."""
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Find all test database files (both commis and e2e patterns)
    patterns = [os.path.join(script_dir, "test_commis_*.db"), os.path.join(script_dir, "test_e2e_*.db")]

    db_files = []
    for pattern in patterns:
        db_files.extend(glob.glob(pattern))

    if not db_files:
        print("No test database files found to clean up.")
        return 0

    print(f"Found {len(db_files)} test database file(s) to clean up:")

    errors = []
    for db_file in db_files:
        try:
            os.remove(db_file)
            print(f"  ✓ Removed: {os.path.basename(db_file)}")
        except Exception as e:
            errors.append((db_file, str(e)))
            print(f"  ✗ Failed to remove {os.path.basename(db_file)}: {e}")

    if errors:
        print(f"\nFailed to remove {len(errors)} file(s)")
        return 1

    print(f"\nSuccessfully cleaned up {len(db_files)} test database file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(cleanup_test_databases())
