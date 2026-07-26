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
python eval/recall/run_eval.py --strategy fts
python eval/recall/run_eval.py --strategy fts --verbose
```

Requires a device token at `~/.longhouse/machine/device-token`, or
`LONGHOUSE_EVAL_TOKEN` and `LONGHOUSE_EVAL_URL`.

## Rules

**Label before implementing.** Queries added after seeing a strategy's results
are tuning, not evaluation.

**Exclude the sessions that built this.** Work on recall discusses recall, so
those sessions match everything about retrieval and would inflate every score.
`excluded_sessions` in `queries.jsonl` holds them.

**Gold is a session id today.** When trace boundaries exist, gold becomes a trace
id and recall gets stricter — a session can be 8,000 events, so finding the right
session is a weaker claim than finding the right moment.
