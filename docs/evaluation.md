# Retrieval Evaluation

Retrieval quality is treated as an engineering metric. The evaluator is repository tooling, not part of the MemBus runtime API.

## Dataset

The baseline dataset is:

```text
evals/golden_v1.json
```

It covers:

- exact issue IDs
- code symbols
- file paths
- tool preferences
- deterministic aliases
- workspace/project/repo isolation
- type filtering
- incidents/procedures
- Chinese full substring and reordered/separated Chinese term queries

V1 target additions:

- English morphology (`retries` / `retry`, `deploy` / `deployment` through the word-part fallback)
- natural-language questions whose stopwords must not crowd out the real terms
- superseded memories: the successor is expected, the predecessor must not appear
- duplicate puts: identical content in the same context yields one memory
- `list` context applicability (global + matching returned, conflicting/narrower excluded, inactive opt-in)

Dataset format: a `memories` entry may carry `supersedes: [keys]` or be `{key, same_as}` (re-puts that memory and must get the same ID back); a query may carry `absent: [keys]` (superseded-leak check) and `strict`; `list_cases` check `MemBus.list` with `expected` / `absent` keys.

Grow the set from real retrieval failures toward ≥ 100 queries; a 26-query self-authored set at 1.0 recall proves little.

## Isolation

`tools/evaluate.py` creates a temporary MemBus database, seeds fixture memories and aliases, runs queries, then removes the database. It never writes to the user's store.

## Metrics

- **Recall@5 / Recall@10** — expected memories retrieved in Top-K.
- **MRR** — rank of the first expected memory.
- **Zero-result rate** — queries returning nothing.
- **Context-preference error rate** — a lower-priority context result ranking ahead of an expected preferred-context result.
- **Superseded-leak rate** — queries where a superseded memory appears in results. Gate: 0.
- **Latency P50 / P95** — in-process `MemBus.search` regression indicators.

## Scale run

`--scale N` bulk-seeds N synthetic memories (English sentences, ticket/file identifiers, Chinese sentences, across workspaces/projects/repos) that are **newer** than the fixtures, so they win exact-match recency ties. It reports latency and the same recall metrics; list cases are skipped (synthetic rows crowd the fixtures out of a page). Gate with `--max-p95-ms`. CI runs 100k with a looser 100 ms bound for shared runners; the design target is 50 ms.

Measured at N = 100000 (laptop): P95 ≈ 26 ms; recall@10 = 0.97. The one miss is `how should we deploy`: the word-part fallback only runs while results are short, and thousands of synthetic memories containing `deployment` out-rank the fixture. That is the documented soft spot, not a regression.

## Run

```bash
python -m pip install -e '.[dev]'

python tools/evaluate.py evals/golden_v1.json
```

Apply CI-style gates:

```bash
python tools/evaluate.py evals/golden_v1.json \
  --min-recall-5 0.95 \
  --min-recall-10 0.95 \
  --max-context-preference-error-rate 0.0 \
  --max-superseded-leak-rate 0.0
```

A failed threshold exits with status 2.

## Regression policy

When a real query fails:

1. reduce it to the smallest representative fixture;
2. add stable expected/lower-priority keys;
3. prove the case fails before changing retrieval;
4. fix retrieval;
5. keep the regression permanently.

The purpose of the golden set is to make new complexity falsifiable. Query rewrite, reranking, embeddings, graph retrieval, or other machinery should not enter the runtime unless the dataset demonstrates a meaningful gain.
