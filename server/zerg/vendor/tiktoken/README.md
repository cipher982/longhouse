Vendored `tiktoken` encoding blobs.

Why these files exist:
- `tiktoken` installs library code but lazily downloads encoding tables on first use.
- Longhouse uses `tiktoken` in the backend for transcript token counting and truncation.
- We vendor the exact supported encodings so local dev, tests, CI, and hosted tenants do not depend on first-use network fetches.

What is intentionally vendored:
- `cl100k_base`
- `o200k_base`

Why the filenames are hashes:
- `tiktoken` caches by SHA-1 of the source URL.
- We keep the upstream-compatible filenames so runtime cache seeding is a direct file copy.

Policy:
- This directory is read-only seed data, not a writable cache.
- Runtime copies these files into the OS cache directory before `tiktoken` uses them.
