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
- Commit `131a9ebd` gates both Claude ship paths on a parser-ready transcript and tightens the early retry ladder to `0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4, 6, 8`.
  - Local verification: focused continuation/hook slice passed (`41 passed`) and `make test` passed (`1180 passed`).
  - Post-deploy verification: `make qa-live` passed (`11 passed`) and hosted managed-local Claude stress passed (`6/6`) on session `fb1e89e1-3388-4556-88b0-5d5d8865ab5c`.
  - Result: still noisy, mostly lateral. `pre_enqueue_latency_ms` was `1583`, `768`, `1075`, `810`, `1487`, `490` (avg `1036ms`, warm avg `926ms`), and `review_latency_ms` was `2828`, `1948`, `2026`, `1538`, `2109`, `1348` (avg `1966ms`, warm avg `1794ms`).
  - The ledger still clustered `terminal_to_durable_ms` near the retry checkpoints: `1307`, `489`, `791`, `506`, `1101`, `118`.
- Commit `9d0e4d03` reduced `/sessions/{id}/chat` managed-local poll/grace timings (`MANAGED_LOCAL_POLL_INTERVAL_SECS=0.1`, `MANAGED_LOCAL_PRE_FORCE_SYNC_GRACE_SECS=0.1`).
  - Local verification: focused continuation/control slice passed (`36 passed`) and `make test` passed (`1180 passed`).
  - Post-deploy verification: first `make qa-live` rerun flaked once on the initial timeline auth reload during warmup (`10/11`), immediate rerun passed (`11 passed`), and hosted managed-local Claude stress passed (`6/6`) on session `fccc4083-7428-4c9f-aa6f-45ad10d9e58c`.
  - Result: no material improvement. `pre_enqueue_latency_ms` was `1908`, `1255`, `384`, `1159`, `1478`, `421` (avg `1101ms`, warm avg `939ms`), and `review_latency_ms` was `4675`, `2474`, `1324`, `1941`, `2562`, `1508` (avg `2414ms`, warm avg `1962ms`).
  - `terminal_to_durable_ms` remained the dominant noisy segment: `1407`, `986`, `109`, `844`, `1180`, `138`.
- Current conclusion: queue claim and controller runtime are no longer the primary problem, and simple route/poll timing tuning is not enough. The next bounded slice should move closer to first principles:
  - either ship immediately on transcript-ready detection instead of sampling coarse retry checkpoints
  - or teach `longhouse-engine ship --file` to resolve/replay its own queued gap for the target path before returning a no-op
