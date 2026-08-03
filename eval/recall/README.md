# Recall Evaluation

Measures whether recall finds evidence that exists. Latency is measured
elsewhere; this answers the question benchmarks cannot: **when the answer is in
history, does search return it?**

That is the failure in `docs/specs/cross-session-recall-postmortem.md` — an agent
concluded nothing existed when the answer was sitting in an earlier session.

## Why this exists

Ranking benchmarks compare a change against the previous ranking, which only
detects regressions. They cannot tell you whether the ranking is any good. Every
retrieval decision so far — candidate ceilings, recency bounds, whether to add
embeddings — has been argued from latency numbers and BM25 self-agreement. This
set makes retrieval quality falsifiable.

## Labelling method

Gold labels are mechanically derived, not judged:

1. Find a session using a **distinctive exact term** it certainly contains
   (`crisismerge`, `kopia`, `--codex-bin`).
2. Confirm the returned evidence really covers the topic.
3. Write the query as a **paraphrase that omits those exact terms**.

The query therefore cannot be answered by matching the string that located the
gold. That is the paraphrase gap, and it is what dense retrieval is supposed to
close. Labels stay defensible because "session X contains the ADB setup" is a
fact about the corpus rather than an opinion about relevance.

`absent` queries are the opposite control: plausible, on-topic, and answered by
nothing in the corpus. They exist because a retriever that returns something for
everything is as broken as one that returns nothing.

## Categories

| category | asks | catches |
| --- | --- | --- |
| `exact` | identifiers, flags, errors | dense retrieval regressing lexical strengths |
| `paraphrase` | "have we done X?" in other words | the postmortem failure |
| `causal` | "why did we reject X?" | whether summaries earn their keep |
| `supersession` | "what is it *now*?" | stale answers winning |
| `absent` | things that never happened | false confidence |

## Metrics

`false_negative_rate` is the release gate: answer-present queries where no gold
session appeared. Recall@k is diagnostic — it says how much better a reranker
could do, since a reranker cannot recover what first-stage retrieval missed.

## Running

```bash
python eval/recall/run_eval.py --strategy lexical
python eval/recall/run_eval.py --strategy semantic
python eval/recall/run_eval.py --strategy auto --expected-sha "$SHIPPED_SHA" --verbose
```

Requires a device token at `~/.longhouse/machine/device-token`, or
`LONGHOUSE_EVAL_TOKEN` and `LONGHOUSE_EVAL_URL`.

The command is a release gate, not a reporting-only benchmark. It requests 25
results and exits nonzero for any request error, incorrect lane attribution,
false-negative rate above the full-corpus Qwen3-8B @256d baseline (36 misses in
76 answerable queries, or 47.4%), recall@5 below that baseline, or a regression
against its category floors at k=25 (exact 11, paraphrase 16, causal 7,
supersession 6). JSON output includes
the exact endpoint, git SHA, query-set digest, thresholds, lane contract, and
embedding model/revision observed from the live response.
For dense strategies it also requires a complete corpus certificate on every
response and records the projector, catalog watermark range, session/episode
count ranges, zero-defect resident invariants, and per-query error details. A
missing/incomplete certificate or mixed embedding space/projector fails the run.
Every response must also report the exact serving commit requested with
`--expected-sha` (the evaluator checkout SHA by default), so a split deployment
cohort cannot produce one blended quality score.

## Rules

**Label before implementing.** Queries added after seeing a strategy's results
are tuning, not evaluation.

**Exclude the sessions that built this.** Work on recall discusses recall, so
those sessions match everything about retrieval and would inflate every score.
`excluded_sessions` in `queries.jsonl` holds them.

**Gold is a session id today.** When trace boundaries exist, gold becomes a trace
id and recall gets stricter — a session can be 8,000 events, so finding the right
session is a weaker claim than finding the right moment.

## Historical results: partial-corpus lexical vs. hybrid (2026-07-26)

These measurements predate the full-corpus 89-query gate above. The
`mode=auto` endpoint default fuses lexical (storage-v2/searchd FTS) with dense
episode embeddings (Qwen3-8B, 256 dims, one vector per user-request episode) via
reciprocal rank fusion. `run_eval.py` always calls the endpoint default, so both
rows below come from the same script — the only variable is whether the dense
lane behind it has embeddings to draw on.

|                        | lexical-only baseline | hybrid, ~93% of corpus embedded |
| ---------------------- | ---------------------:| --------------------------------:|
| false 'nothing found'  | 73.7%                 | 67.1%                            |
| exact                  | 17-18/27              | 17/27                            |
| paraphrase             | 1/24 (4%)             | 5/24 (21%)                       |
| causal                 | 0/15                  | 1/15                             |
| supersession           | 1/10                  | 2/10                             |
| absent                 | 1/13                  | 1/13                             |

Paraphrase recall — the failure mode in `cross-session-recall-postmortem.md` —
improved 5x. Exact-match held steady, confirming dense search didn't regress
lexical's strength on identifiers/flags. Causal and supersession moved off a
0-signal floor. This is real, measured improvement, not a fix: paraphrase/causal
recall is still under 25%. Full implementation history (chunking design, the
storage-v2 rebuild after the legacy archive DB turned out to be empty, two Sol
review passes, and the backfill bugs found finishing it) lived in
`docs/specs/dense-recall-embeddings.md`, deleted per that doc's own instruction
once this section captured the result — see git history for the working notes.
