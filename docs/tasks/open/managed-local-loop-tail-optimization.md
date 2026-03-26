# Managed-Local Loop Tail Optimization

Status: In progress
Owner: Codex
Last updated: 2026-03-26

## Goal

Use the now-persisted managed-local Loop timing trail to shave the remaining hosted review tail without reopening the correctness work that is already green.

Current focus:

- reduce the remaining pre-enqueue latency from assistant-finished to `turn_loop` enqueue
- keep claim latency low while focusing on ship/ingest variance
- keep the work narrow and measurement-driven

## Done when

- Fresh hosted smoke runs show `review_latency_ms` reliably in low single-digit seconds.
- The dominant remaining tail is identified with real before/after numbers.
- At least one bounded backend change lands with targeted tests.
- The task notes record the latest hosted timings on `david010`.

## Checklist

- [x] Close the stale profiling task and capture a fresh prod baseline
- [x] Run a fresh multi-turn smoke and compare first-turn vs steady-state timings
- [x] Pick the biggest remaining latency bucket and implement one bounded optimization slice
- [x] Add or update targeted tests around the chosen latency path
- [x] Re-run hosted smoke and record before/after timings

## Notes

- Correctness is green; do not reopen `/sessions/{id}/chat` completion semantics unless new timings force it.
- Current `david010` baseline from session `1e2741e5-dcbb-460e-89c8-449680a65b9d`: `pre_enqueue_latency_ms=870`, `claim_latency_ms=404`, `controller_latency_ms=899`, `worker_latency_ms=917`, `review_latency_ms=2189`, `processing_latency_ms=1321`.
- Prefer one-variable-at-a-time changes. The point of this slice is to avoid another blind reliability thrash.
- Commit `a95f8753` wakes the hot `turn_loop` worker immediately on enqueue instead of waiting for the next poll interval.
- Local verification after the change: `make test` (`1164 passed`).
- Fresh steady-state hosted run on `david010` after deploy/reprovision: session `1f39af67-74c6-41d3-8a56-eb112900a290`.
- Warm-tenant review timings after the wakeup change:
  - turn 1: `pre_enqueue_latency_ms=1125`, `claim_latency_ms=8`, `review_latency_ms=2302`
  - turn 2: `pre_enqueue_latency_ms=877`, `claim_latency_ms=8`, `review_latency_ms=1880`
  - turn 3: `pre_enqueue_latency_ms=1031`, `claim_latency_ms=9`, `review_latency_ms=1969`
  - turn 4: `pre_enqueue_latency_ms=1859`, `claim_latency_ms=7`, `review_latency_ms=2907`
  - turn 5: `pre_enqueue_latency_ms=2352`, `claim_latency_ms=11`, `review_latency_ms=3359`
  - turn 6: `pre_enqueue_latency_ms=864`, `claim_latency_ms=14`, `review_latency_ms=1780`
- Main improvement from this slice: steady-state claim latency dropped from roughly `119-430ms` to `7-14ms`.
- Remaining tail is now clearly pre-enqueue / first-ship latency, not worker claim latency. The cold first session immediately after reprovision (`8c907c51-274e-4223-a134-421fec381487`) still produced a large first-turn outlier, so the next slice should target warmup / pre-enqueue behavior rather than the queue worker.
- Commit `ee50815a` shared and densified the managed-local Claude ship retry ladder for both the Stop hook and the direct ship command. Absolute attempts are now `0, 0.1, 0.25, 0.5, 1, 1.5, 2, 3, 4, 6, 8` seconds.
- Commit `e1042e04` starts the Claude Stop ship loop before the synchronous presence POST so transcript shipping no longer inherits that network round-trip on the hot path.
- Local verification on the final combined branch state: focused hook/control tests passed (`26 passed`) and `make test` passed (`1166 passed`).
- Post-deploy verification on the final branch state: `make qa-live` passed (`11 passed`) and hosted managed-local Claude stress passed twice (`6/6` both times).
- Fresh hosted run after `ee50815a`: session `54385d04-ed3f-475d-8cea-d7fb58cd4033`.
  - `pre_enqueue_latency_ms`: `1702`, `1207`, `552`, `910`, `448`, `1275`
  - `review_latency_ms`: `2795`, `2073`, `1597`, `1596`, `1151`, `1901`
  - Average `pre_enqueue_latency_ms` dropped to about `1016ms` from the earlier warm baseline of about `1351ms`.
  - Average `review_latency_ms` dropped to about `1852ms` from the earlier warm baseline of about `2366ms`.
- Fresh hosted run after `e1042e04`: session `635b0c38-d302-4dad-8322-56c3bc842014`.
  - First-turn `pre_enqueue_latency_ms` improved from `1702ms` on the prior fresh-after-reprovision run to `868ms`.
  - The same run was still noisy on later turns (`761-1669ms` sampled pre-enqueue), so this helped the cold first-turn path more than steady-state variance.
- Additional warm follow-up after `e1042e04`: session `0eb7e67d-ffe2-4e46-aaac-41d8d483c18e`.
  - Sampled `pre_enqueue_latency_ms`: `879`, `1697`, `785`, `2401`, `1739`
  - Warm steady-state variance is still real, so the remaining bottleneck is still pre-enqueue/ship variability, not claim latency or controller runtime.
- Fresh baseline before the latest producer-side pass: session `6426e181-c635-44f8-b464-a4658294f5b0`.
  - `pre_enqueue_latency_ms`: `1722`, `905`, `1040`, `862`, `1129`, `431` (avg `1015ms`)
  - `review_latency_ms`: `2593`, `1632`, `1781`, `1617`, `1957`, `1314` (avg `1816ms`)
  - `terminal_to_durable_ms` from `managed_local_turns`: `1408`, `540`, `751`, `471`, `845`, `145`
- Commit `131a9ebd` initially added a parser-ready gate and tightened the early retry ladder to `0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4, 6, 8`.
  - Local verification: focused continuation/hook slice passed (`41 passed`) and `make test` passed (`1180 passed`).
  - Post-deploy verification: `make qa-live` passed (`11 passed`) and hosted managed-local Claude stress passed (`6/6`) on session `fb1e89e1-3388-4556-88b0-5d5d8865ab5c`.
  - Result: still noisy, mostly lateral. `pre_enqueue_latency_ms` was `1583`, `768`, `1075`, `810`, `1487`, `490` (avg `1036ms`, warm avg `926ms`), and `review_latency_ms` was `2828`, `1948`, `2026`, `1538`, `2109`, `1348` (avg `1966ms`, warm avg `1794ms`).
  - The ledger still clustered `terminal_to_durable_ms` near the retry checkpoints: `1307`, `489`, `791`, `506`, `1101`, `118`.
- Follow-up review found that the strict parser-ready gate was too aggressive for the engine parser contract: a partial EOF line should not block shipping already-complete earlier lines.
  - Commit `7bc5661b` removes that gate while keeping the denser retry ladder.
  - Local verification after the revert: focused continuation/hook slice passed (`41 passed`) and `make test` passed (`1180 passed`).
  - Post-deploy verification after the revert: first `make qa-live` run hit the same warmup flake on the initial timeline test, immediate rerun passed (`11 passed`), and a one-turn hosted managed-local Claude stress run passed on session `f95f199e-beb6-4dc0-a3a9-5688926018ce`.
  - That post-revert session recorded `pre_enqueue_latency_ms=849`, `claim_latency_ms=12`, `controller_latency_ms=843`, `worker_latency_ms=861`, `review_latency_ms=1720`.
  - Full warmed follow-up after the revert: session `1f01642a-8202-48cc-ae79-70675fa9447b`.
    - `pre_enqueue_latency_ms`: `1412`, `1126`, `766`, `558`, `863`, `1328` (avg `1009ms`, warm avg `928ms`)
    - `review_latency_ms`: `3633`, `1931`, `1674`, `1408`, `1845`, `2352` (avg `2140ms`, warm avg `1842ms`)
    - `claim_latency_ms` stayed low outside the first turn (`35`, `29`, `36`, `20`, `11` warm).
- Commit `9d0e4d03` reduced `/sessions/{id}/chat` managed-local poll/grace timings (`MANAGED_LOCAL_POLL_INTERVAL_SECS=0.1`, `MANAGED_LOCAL_PRE_FORCE_SYNC_GRACE_SECS=0.1`).
  - Local verification: focused continuation/control slice passed (`36 passed`) and `make test` passed (`1180 passed`).
  - Post-deploy verification: first `make qa-live` rerun flaked once on the initial timeline auth reload during warmup (`10/11`), immediate rerun passed (`11 passed`), and hosted managed-local Claude stress passed (`6/6`) on session `fccc4083-7428-4c9f-aa6f-45ad10d9e58c`.
  - Result: no material improvement. `pre_enqueue_latency_ms` was `1908`, `1255`, `384`, `1159`, `1478`, `421` (avg `1101ms`, warm avg `939ms`), and `review_latency_ms` was `4675`, `2474`, `1324`, `1941`, `2562`, `1508` (avg `2414ms`, warm avg `1962ms`).
  - `terminal_to_durable_ms` remained the dominant noisy segment: `1407`, `986`, `109`, `844`, `1180`, `138`.
- Current conclusion: queue claim and controller runtime are no longer the primary problem, and simple route/poll timing tuning is not enough. The next bounded slice should move closer to first principles:
  - either ship immediately on transcript-ready detection instead of sampling coarse retry checkpoints
  - or teach `longhouse-engine ship --file` to resolve/replay its own queued gap for the target path before returning a no-op
- Commit `e9ea245e` shipped the engine-side version of that second option:
  - explicit `longhouse-engine ship --file <path>` now recovers queued gaps for the target file, replays pending spool entries for that file immediately, and preserves the spooled `session_id` override during replay
  - commit `5edc83eb` updated `.github/workflows/runtime-image.yml` so future `engine/**` changes trigger the runtime image build automatically
  - local verification after the engine slice: `make test` passed (`1181 passed`)
  - fresh single-turn hosted verification after deploy/reprovision: session `ec133057-7e17-4fa9-8ddb-bf0cd8a5b912` recorded `pre_enqueue_latency_ms=919`, `claim_latency_ms=12`, `controller_latency_ms=772`, `worker_latency_ms=791`, `review_latency_ms=1717`
- The first multi-turn hosted run after the engine change exposed a new route-side edge:
  - session `2ed5aecd-8645-4c6d-b8e3-b68bf483cfc5` timed out on turn 4 even though the turn later became durable and reviewed
  - root cause: `/api/sessions/{id}/chat` could observe durable current-turn events, cancel the pending terminal waiter, and leave the ledger without `terminal_at`, which then inflated downstream review settle latency
  - commit `6a3cdd6a` now waits briefly for the terminal hook after durable current-turn events arrive
  - follow-up review found that terminal-waiter exceptions should stay non-fatal once durability is present, so commit `cf69022a` now swallows timeout/error/cancel from that grace wait and keeps the durable reply path alive
  - local verification after the hardening slice: `make test` passed (`1182 passed`)
- Fresh hosted verification after the route fix: session `a968f41e-9a53-4bce-a99a-f5353812a926`.
  - hosted managed-local Claude stress passed `6/6`; every turn returned `api_done=1`, `api_timed_out=0`, `sync_status=complete`, `control_status=completed`
  - `turn_reviews` for that session recorded:
    - turn 1: `pre_enqueue_latency_ms=938`, `claim_latency_ms=25`, `controller_latency_ms=1592`, `worker_latency_ms=1630`, `review_latency_ms=2580`
    - turn 2: `pre_enqueue_latency_ms=582`, `claim_latency_ms=18`, `controller_latency_ms=769`, `worker_latency_ms=785`, `review_latency_ms=1382`
    - turn 3: `pre_enqueue_latency_ms=615`, `claim_latency_ms=12`, `controller_latency_ms=762`, `worker_latency_ms=779`, `review_latency_ms=1402`
    - turn 4: `pre_enqueue_latency_ms=881`, `claim_latency_ms=21`, `controller_latency_ms=743`, `worker_latency_ms=761`, `review_latency_ms=1661`
    - turn 5: `pre_enqueue_latency_ms=841`, `claim_latency_ms=39`, `controller_latency_ms=923`, `worker_latency_ms=946`, `review_latency_ms=1818`
    - turn 6: `pre_enqueue_latency_ms=871`, `claim_latency_ms=45`, `controller_latency_ms=696`, `worker_latency_ms=726`, `review_latency_ms=1628`
  - Average `pre_enqueue_latency_ms` is now about `788ms`; warm-turn average is about `758ms`
  - Average `review_latency_ms` is now about `1745ms`; warm-turn average is about `1578ms`
  - `make qa-live` passed on rerun after reprovision; the first run still hit the existing browser reload flake on the opening timeline test, but the backend path stayed healthy throughout
- Follow-up producer-path experiment on 2026-03-26 tried moving transcript-ready waiting fully into `longhouse-engine ship --file`:
  - commit `cda227d9` replaced the shell retry ladder with a single engine-side `--wait-ready-ms` loop and taught the engine to wait for file existence plus reply-ready bytes
  - commit `d83e1a2e` extended that engine-side wait window to preserve the old long-tail coverage
  - local verification stayed green, but hosted behavior did not improve enough to keep:
    - session `659038ea-1058-4d83-a78e-6c4a4a67b0ae` showed large regressions on the first multi-turn run, with `pre_enqueue_latency_ms` of `10136`, `13014`, `7585`, `1605`, `2157`, `2597`
    - single-turn smoke `a79b0e03-eecc-4daa-a2f6-22e3f25bec17` passed, but still recorded `pre_enqueue_latency_ms=1689`, which is worse than the current shipped baseline
    - repeated hosted multi-turn attempts also became more fragile, including durability-poll timeouts in the stress harness
  - verdict: the engine-owned wait loop is a good research direction, but this first implementation regressed the managed-local hot path in prod and is not shipped
- Rollback and current live state:
  - commits `f026e695` and `c9e950aa` reverted the engine-owned wait experiment
  - post-rollback `make qa-live` returned to the usual pattern: first run hit the known opening timeline flake, immediate rerun passed `11/11`
  - post-rollback hosted managed-local continuation is back on the previous baseline path; session `0518e015-c924-4e4f-a9a4-a7697cc3ec8b` recorded `pre_enqueue_latency_ms=526` and `736` on the first two reviewed turns before the harness lost the SSE stream on turn 3 with an HTTP/2 client error even though tmux showed the exact expected reply
  - current conclusion: keep the shipped baseline at the pre-experiment path and continue this task only with smaller producer-side changes plus better per-turn instrumentation, not another broad hot-path rewrite
- Commit `be593ce2` tightened the explicit managed-local file sync path so `longhouse-engine ship --file` only advances offsets when unread transcript content includes assistant/tool reply evidence.
  - Local verification after that slice: engine + managed-local focused tests passed and `make test` passed (`1189 passed` before the new route regressions landed).
  - Post-deploy, the first hosted continuation run exposed a follow-on route gap: if the early pre-terminal direct ship exhausted its retry ladder with the recoverable no-op (`exit_code=13`, "did not ship new events"), `/api/sessions/{id}/chat` could sit on pending durability until a much later background ship rescued the turn.
- Commit `3a60d665` fixes that route gap without widening retries to hard failures:
  - `/api/sessions/{id}/chat` now retries the direct Claude sync after terminal only when the earlier ship already finished with the specific recoverable no-op outcome.
  - Hard failures like missing runner metadata or missing `longhouse-engine` do not get retried.
  - Targeted regression coverage now includes both cases in `tests_lite/test_managed_local_session_chat.py`, and `make test` passed (`1191 passed`).
- Fresh hosted verification after `3a60d665`:
  - Deploy path completed cleanly: GHCR build `23600517435` passed, marketing + control plane redeployed, `david010` reprovisioned, health stayed `healthy` with `write_serializer.errors = 0`.
  - `make qa-live` passed (`11 passed`).
  - Single-turn hosted managed-local Claude stress with the default 30s timeout passed on session `afc8f7c0-6818-49b9-b398-cfd456510ccd`.
  - A cold multi-turn stress run immediately after reprovision still hit a launch-path `httpx.ReadTimeout` on `POST /api/sessions/managed-local/this-device` while the server created the session anyway. That looks separate from continuation durability and should be tracked as launch warmup noise unless it starts reproducing outside the immediate post-reprovision window.
  - Warmed 6-turn hosted managed-local Claude stress with `--chat-timeout-secs 60` passed `6/6` on session `4a102ba6-9098-49ac-9f3d-94ba7763aa4c`; every turn returned `api_done=1`, `api_timed_out=0`, `sync_status=complete`, `control_status=completed`.
  - Review timings on that warmed run:
    - turn 1: `pre_enqueue_latency_ms=905`, `claim_latency_ms=15`, `controller_latency_ms=1287`, `review_latency_ms=2221`
    - turn 2: `pre_enqueue_latency_ms=622`, `claim_latency_ms=11`, `controller_latency_ms=1673`, `review_latency_ms=2317`
    - turn 3: `pre_enqueue_latency_ms=877`, `claim_latency_ms=46`, `controller_latency_ms=675`, `review_latency_ms=1612`
    - turn 4: `pre_enqueue_latency_ms=1305`, `claim_latency_ms=13`, `controller_latency_ms=724`, `review_latency_ms=2057`
    - turn 5: `pre_enqueue_latency_ms=980`, `claim_latency_ms=42`, `controller_latency_ms=803`, `review_latency_ms=1836`
    - turn 6: `pre_enqueue_latency_ms=25231`, `claim_latency_ms=37`, `controller_latency_ms=966`, `review_latency_ms=26252`
- Current conclusion after the route fix:
  - The new correctness hole is closed; the hot path no longer falls back to `sync_status=pending` just because the early direct ship no-op'd before terminal.
  - The remaining tail is still producer-side and now more specific: turn 6 on `4a102ba6-9098-49ac-9f3d-94ba7763aa4c` accepted the send at `14:53:19.146Z`, but the turn ledger did not stamp `terminal_at` / `durable_at` until `14:53:50.5Z` even though the final assistant event timestamp inside the transcript was `14:53:25.316Z`.
  - That points to the next slice more clearly: instrument or reduce the gap between "assistant reply visible in the TUI / transcript timestamped" and "reply evidence becomes durably shippable," rather than doing more queue/controller tuning.
