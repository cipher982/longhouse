# E2E Test Debugging & Infrastructure: Lessons Learned

## Context

- The Zerg Agent Platform has a comprehensive E2E test suite using Playwright, targeting a Rust/WASM frontend and a FastAPI backend.
- Historically, E2E tests were run against a backend that used the developer's `.env` file and whatever database was configured—often the dev or even production DB.
- This led to tests passing or failing based on polluted, non-deterministic data, and posed a risk of data loss or corruption.

## What We Did

- Investigated why E2E tests were failing, especially tests that create or edit agents.
- Discovered that tests were relying on pre-existing data in the database, which is unsafe and non-reproducible.
- Updated the E2E test runner to always start the backend with `TESTING=1`, ensuring a clean, disposable test database for every run.
- Ensured the backend is started from the correct directory using `uv run python -m uvicorn ...` as required by project conventions.
- Confirmed that with a clean DB, almost all tests fail due to missing data, revealing hidden dependencies on polluted state.

## What We Learned

- **E2E tests must be self-contained:**
  Every test must create all the data it needs. Never assume any agents, threads, or users exist at test startup.
- **Test DB isolation is critical:**
  Running tests against a clean, disposable database is the only way to guarantee safety and reproducibility.
- **Legacy tests may "pass" for the wrong reasons:**
  If tests pass only because of leftover data, they are not reliable.
- **Test runner scripts must always use the correct backend startup method and environment.**
- **Playwright's `webServer` config and custom scripts must both ensure backend is started in test mode.**

## How We Should Approach E2E Testing

1. **Test Isolation:**
   - Always use a disposable test database for E2E runs.
   - Never use `.env` or production/dev DBs for automated tests.

2. **Test Data Setup:**
   - Each test (or `beforeEach` hook) should create all required data (agents, threads, etc.).
   - Never rely on pre-existing data.

3. **Test Runner Best Practices:**
   - Use `uv run python -m uvicorn ...` from the backend directory.
   - Set `TESTING=1` and any other flags needed for a clean test environment.
   - Ensure backend and frontend are both started in test/dev mode.

4. **Debugging:**
   - If a test fails, check if it is missing required setup data.
   - Use network interception and logging in Playwright to debug backend responses.

5. **Documentation:**
   - Document the test runner setup and requirements in the project README and E2E README.
   - Make it clear that tests must be self-contained and never use real data.

## Next Steps

- Refactor all E2E tests to be self-contained and create their own data.
- Remove any assumptions about pre-existing agents, threads, or users.
- Continue to use the improved test runner script for all E2E runs.
- Periodically reset and verify the test DB isolation to prevent accidental data leaks.

---

**Summary:**
Moving to a clean, isolated test database for E2E runs is the only way to ensure safe, reliable, and reproducible tests. All tests must be rewritten to create their own data and never depend on polluted state. This is a critical step for any serious software project.
