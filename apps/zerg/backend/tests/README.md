# Legacy Backend Test Suite

This directory contains the **legacy Postgres-heavy suite** built for the original enterprise deployment.

As of 2026-01-31, the default backend tests live in `tests_lite/` and run against SQLite to unblock the OSS pivot.

Use `make test-legacy` (or `./run_backend_tests.sh`) to run this suite.
